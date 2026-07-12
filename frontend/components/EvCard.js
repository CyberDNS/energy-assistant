import { defineComponent, ref, computed } from 'vue'
import {
  fetchEvPlan, saveEvWeeklyPlan, setEvDayOverride, clearEvDayOverride,
  startForceCharge, stopForceCharge, disableChargepoint, enableChargepoint,
} from '../api.js'
import { markPlanning } from '../composables/useStatus.js'

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

function fmt(v, dec = 0) {
  return v == null ? '—' : Number(v).toFixed(dec)
}

function fmtDeadline(isoStr) {
  if (!isoStr) return null
  return new Date(isoStr).toLocaleString(undefined, {
    weekday: 'short', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

function dayLabel(dateStr) {
  const d = new Date(dateStr + 'T00:00:00')
  const wd = d.toLocaleDateString(undefined, { weekday: 'short' }).slice(0, 2)
  return `${wd} ${d.getDate()}`
}

export default defineComponent({
  name: 'EvCard',
  props: { ev: { type: Object, required: true } },
  emits: ['refresh'],

  setup(props, { emit }) {
    const busy   = ref(false)
    const errMsg = ref(null)

    // Day editor (opens when a strip chip is clicked)
    const editDay     = ref(null)   // the day object from ev.next_days
    const editDaySoc  = ref(90)
    const editDayTime = ref('06:00')

    // Weekly plan editor (collapsible)
    const weeklyOpen  = ref(false)
    const weeklyRows  = ref([])     // [{weekday, enabled, target_soc_pct, target_by}]
    const weeklyDirty = ref(false)

    // Force charge dialog
    const forceOpen = ref(false)
    const forceSoc  = ref(props.ev.charge_limit_soc_pct ?? 90)

    const forceActive = computed(() => props.ev.force_charge != null)

    const socColor = computed(() => {
      const soc = props.ev.soc_pct
      if (soc == null) return 'var(--accent)'
      const target = props.ev.goal?.target_soc_pct ?? props.ev.charge_limit_soc_pct
      if (soc >= target) return 'var(--ok)'
      if (soc < 30)      return 'var(--bad)'
      return 'var(--accent)'
    })

    async function withBusy(fn) {
      busy.value   = true
      errMsg.value = null
      try {
        await fn()
        // Every mutation kicks off a plan re-solve server-side — flag it
        // immediately so the UI shows the recalculating indicator without
        // waiting for the next SSE status push.
        markPlanning()
        emit('refresh')
        // Second refresh shortly after, without keeping the buttons blocked:
        // catches state the server derives asynchronously (fresh goals etc.).
        setTimeout(() => emit('refresh'), 2000)
      } catch (e) {
        errMsg.value = e.message
      } finally {
        busy.value = false
      }
    }

    // ── Next-7-days strip ────────────────────────────────────────────
    function openDayEditor(day) {
      if (editDay.value?.date === day.date) { editDay.value = null; return }
      editDay.value     = day
      editDaySoc.value  = day.target_soc_pct ?? props.ev.charge_limit_soc_pct ?? 90
      editDayTime.value = day.target_by ?? '06:00'
    }

    const onSkipDay = () => withBusy(async () => {
      await setEvDayOverride(props.ev.asset_id, editDay.value.date, { skip: true })
      editDay.value = null
    })

    const onSaveDay = () => withBusy(async () => {
      await setEvDayOverride(props.ev.asset_id, editDay.value.date, {
        skip: false,
        target_soc_pct: Number(editDaySoc.value),
        target_by: editDayTime.value,
      })
      editDay.value = null
    })

    const onRevertDay = () => withBusy(async () => {
      await clearEvDayOverride(props.ev.asset_id, editDay.value.date)
      editDay.value = null
    })

    // ── Weekly plan editor ───────────────────────────────────────────
    async function toggleWeekly() {
      if (weeklyOpen.value) { weeklyOpen.value = false; return }
      busy.value = true
      errMsg.value = null
      try {
        const plan = await fetchEvPlan(props.ev.asset_id)
        weeklyRows.value = plan.weekly
        weeklyDirty.value = false
        weeklyOpen.value = true
      } catch (e) {
        errMsg.value = e.message
      } finally {
        busy.value = false
      }
    }

    const onSaveWeekly = () => withBusy(async () => {
      await saveEvWeeklyPlan(props.ev.asset_id, weeklyRows.value.map(r => ({
        weekday: r.weekday,
        enabled: r.enabled,
        target_soc_pct: Number(r.target_soc_pct),
        target_by: r.target_by,
      })))
      weeklyDirty.value = false
      weeklyOpen.value = false
    })

    // ── Force charge ─────────────────────────────────────────────────
    function openForce() {
      forceSoc.value = props.ev.charge_limit_soc_pct ?? 90
      forceOpen.value = true
    }

    const onStartForce = () => withBusy(async () => {
      await startForceCharge(props.ev.asset_id, Number(forceSoc.value))
      forceOpen.value = false
    })

    const onStopForce = () => withBusy(() => stopForceCharge(props.ev.asset_id))

    const onDisable = () => withBusy(() => disableChargepoint(props.ev.asset_id))
    const onEnable  = () => withBusy(() => enableChargepoint(props.ev.asset_id))

    return {
      busy, errMsg, socColor, fmt, fmtDeadline, dayLabel, WEEKDAYS,
      editDay, editDaySoc, editDayTime, openDayEditor, onSkipDay, onSaveDay, onRevertDay,
      weeklyOpen, weeklyRows, weeklyDirty, toggleWeekly, onSaveWeekly,
      forceOpen, forceSoc, forceActive, openForce, onStartForce, onStopForce,
      onDisable, onEnable,
    }
  },

  template: `
    <div class="ev-card">

      <!-- Header -->
      <div class="ev-card-header">
        <span class="ev-label">
          {{ ev.label }}
          <span class="ev-device-id">{{ ev.device_id }}</span>
        </span>
        <span v-if="forceActive" class="pill pill-warn">⚡ force charging</span>
        <span :class="['pill', ev.connected ? 'pill-ok' : 'pill-off']">
          {{ ev.connected ? 'Connected' : 'Disconnected' }}
        </span>
      </div>

      <!-- SoC bar -->
      <div class="ev-soc-row">
        <span class="ev-soc-num">{{ ev.soc_pct != null ? fmt(ev.soc_pct) + '%' : '—' }}</span>
        <div class="ev-soc-bar">
          <div v-if="ev.soc_pct != null" class="ev-soc-fill"
            :style="{ width: Math.min(ev.soc_pct, 100) + '%', background: socColor }">
          </div>
        </div>
        <span class="ev-soc-limit">limit {{ fmt(ev.charge_limit_soc_pct) }}%</span>
      </div>

      <!-- Active goal summary -->
      <div class="ev-goal">
        <template v-if="forceActive">
          Force charging to <strong>{{ fmt(ev.force_charge.target_soc_pct) }}%</strong> at full speed
        </template>
        <template v-else-if="ev.goal">
          Target <strong>{{ fmt(ev.goal.target_soc_pct) }}%</strong>
          by <strong>{{ fmtDeadline(ev.goal.target_by) }}</strong>
        </template>
        <template v-else>No active goal — PV-only charging</template>
        <span v-if="ev.disabled" class="pill pill-off">disabled</span>
      </div>

      <!-- Next 7 days strip -->
      <div class="ev-days">
        <button v-for="(day, i) in (ev.next_days ?? [])" :key="day.date"
          :class="['ev-day-chip', 'src-' + day.source,
                   { 'is-passed': day.passed, 'is-editing': editDay?.date === day.date,
                     'is-weekend': day.weekday >= 6, 'is-today': i === 0 }]"
          :disabled="busy"
          @click="openDayEditor(day)">
          <span class="ev-day-name">{{ dayLabel(day.date) }}</span>
          <template v-if="day.source === 'skip'">
            <span class="ev-day-soc ev-day-skip">skip</span>
            <span class="ev-day-time">&nbsp;</span>
          </template>
          <template v-else-if="day.target_soc_pct != null">
            <span class="ev-day-soc">{{ fmt(day.target_soc_pct) }}%</span>
            <span class="ev-day-time">{{ day.target_by }}</span>
          </template>
          <template v-else>
            <span class="ev-day-soc ev-day-none">—</span>
            <span class="ev-day-time">&nbsp;</span>
          </template>
          <span v-if="day.source === 'override' || day.source === 'skip'" class="ev-day-dot"
            title="Modified for this day"></span>
        </button>
      </div>

      <!-- Day editor -->
      <div v-if="editDay" class="ev-day-editor">
        <div class="ev-day-editor-title">
          {{ editDay.date }}
          <span v-if="editDay.source === 'override' || editDay.source === 'skip'" class="pill pill-warn">modified</span>
        </div>
        <div class="ev-inputs">
          <div class="ev-input-group">
            <label>SoC %</label>
            <input type="number" min="0" max="100" step="5" v-model.number="editDaySoc" :disabled="busy" />
          </div>
          <div class="ev-input-group">
            <label>Time</label>
            <input type="time" v-model="editDayTime" :disabled="busy" />
          </div>
        </div>
        <div class="ev-day-editor-actions">
          <button class="btn btn-primary" :disabled="busy" @click="onSaveDay">Save for this day</button>
          <button class="btn btn-neutral" :disabled="busy || editDay.source === 'skip'" @click="onSkipDay">Skip day</button>
          <button class="btn btn-neutral" :disabled="busy || (editDay.source !== 'override' && editDay.source !== 'skip')"
            @click="onRevertDay">Revert to weekly</button>
        </div>
      </div>

      <!-- Actions -->
      <div class="ev-actions">
        <button v-if="forceActive" class="btn btn-danger" :disabled="busy" @click="onStopForce">Stop force charge</button>
        <button v-else class="btn btn-primary" :disabled="busy || !ev.connected || ev.disabled"
          :title="ev.connected ? 'Charge at full speed now' : 'No vehicle connected'"
          @click="openForce">⚡ Charge now</button>
        <button class="btn btn-neutral" :disabled="busy" @click="toggleWeekly">
          {{ weeklyOpen ? '▾' : '▸' }} Weekly plan
        </button>
        <span class="ev-actions-spacer"></span>
        <button v-if="ev.disabled" class="btn btn-success btn-icon" :disabled="busy"
          title="Enable chargepoint" @click="onEnable">⏻</button>
        <button v-else class="btn btn-neutral btn-icon" :disabled="busy"
          title="Disable chargepoint — hands control back to the wallbox" @click="onDisable">⏻</button>
      </div>

      <!-- Force charge dialog -->
      <div v-if="forceOpen && !forceActive" class="ev-force-dialog">
        <label>Charge to</label>
        <input type="number" min="0" max="100" step="5" v-model.number="forceSoc" :disabled="busy" />
        <span>%</span>
        <button class="btn btn-primary" :disabled="busy" @click="onStartForce">Start</button>
        <button class="btn btn-neutral" :disabled="busy" @click="forceOpen = false">Cancel</button>
      </div>

      <!-- Weekly plan editor -->
      <div v-if="weeklyOpen" class="ev-weekly">
        <div class="ev-weekly-grid">
          <div v-for="row in weeklyRows" :key="row.weekday" class="ev-weekly-row">
            <label class="ev-weekly-day">
              <input type="checkbox" v-model="row.enabled" :disabled="busy" @change="weeklyDirty = true" />
              {{ WEEKDAYS[row.weekday - 1] }}
            </label>
            <input type="number" min="0" max="100" step="5" v-model.number="row.target_soc_pct"
              :disabled="busy || !row.enabled" @input="weeklyDirty = true" />
            <span class="ev-weekly-pct">%</span>
            <input type="time" v-model="row.target_by"
              :disabled="busy || !row.enabled" @input="weeklyDirty = true" />
          </div>
          <div class="ev-weekly-actions">
            <button class="btn btn-primary" :disabled="busy || !weeklyDirty" @click="onSaveWeekly">Save weekly plan</button>
          </div>
        </div>
      </div>

      <div v-if="errMsg" class="ev-error">{{ errMsg }}</div>
    </div>
  `,
})
