import { defineComponent, ref, computed, watch } from 'vue'
import { stageEvTarget, enableEvOverride, disableEvOverride, disableChargepoint, enableChargepoint } from '../api.js'

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

function toLocalInputs(isoStr) {
  if (!isoStr) return { date: '', time: '' }
  const d = new Date(isoStr)
  const pad = n => String(n).padStart(2, '0')
  return {
    date: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`,
    time: `${pad(d.getHours())}:${pad(d.getMinutes())}`,
  }
}

function combineToUtcIso(dateStr, timeStr) {
  return new Date(`${dateStr}T${timeStr}:00`).toISOString()
}

export default defineComponent({
  name: 'EvCard',
  props: { ev: { type: Object, required: true } },
  emits: ['refresh'],

  setup(props, { emit }) {
    function initInputs() {
      const source = props.ev.staged ?? props.ev.goal
      const soc = source?.target_soc_pct ?? props.ev.charge_limit_soc_pct
      const dt = toLocalInputs(source?.target_by ?? null)
      if (!dt.date) {
        const d = new Date()
        d.setDate(d.getDate() + 1)
        d.setHours(6, 30, 0, 0)
        const pad = n => String(n).padStart(2, '0')
        dt.date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
        dt.time = `${pad(d.getHours())}:${pad(d.getMinutes())}`
      }
      return { soc, ...dt }
    }

    const init       = initInputs()
    const inputSoc   = ref(init.soc)
    const inputDate  = ref(init.date)
    const inputTime  = ref(init.time)
    const busy       = ref(false)
    const errMsg     = ref(null)
    const stageDirty = ref(false)

    watch([inputSoc, inputDate, inputTime], () => { stageDirty.value = true })

    const overrideActive = computed(() => props.ev.override_active)

    const stagedSummary = computed(() => {
      if (!inputDate.value || !inputTime.value) return null
      try {
        const iso = combineToUtcIso(inputDate.value, inputTime.value)
        return `${fmt(inputSoc.value)}% by ${fmtDeadline(iso)}`
      } catch { return null }
    })

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
        emit('refresh')
        await new Promise(r => setTimeout(r, 2000))
        emit('refresh')
      } catch (e) {
        errMsg.value = e.message
      } finally {
        busy.value = false
      }
    }

    async function onStage() {
      await withBusy(async () => {
        const by = combineToUtcIso(inputDate.value, inputTime.value)
        await stageEvTarget(props.ev.asset_id, inputSoc.value, by)
        stageDirty.value = false
      })
    }

    async function onToggleOverride() {
      await withBusy(async () => {
        if (stageDirty.value && !overrideActive.value) {
          const by = combineToUtcIso(inputDate.value, inputTime.value)
          await stageEvTarget(props.ev.asset_id, inputSoc.value, by)
          stageDirty.value = false
        }
        if (overrideActive.value) {
          await disableEvOverride(props.ev.asset_id)
        } else {
          await enableEvOverride(props.ev.asset_id)
        }
      })
    }

    const onDisable = () => withBusy(() => disableChargepoint(props.ev.asset_id))
    const onEnable  = () => withBusy(() => enableChargepoint(props.ev.asset_id))

    return {
      inputSoc, inputDate, inputTime, stageDirty, stagedSummary,
      busy, errMsg, socColor, overrideActive, fmt, fmtDeadline,
      onStage, onToggleOverride, onDisable, onEnable,
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
        <template v-if="ev.goal">
          Target <strong>{{ fmt(ev.goal.target_soc_pct) }}%</strong>
          by <strong>{{ fmtDeadline(ev.goal.target_by) }}</strong>
        </template>
        <template v-else>No active goal — PV-only charging</template>
        <span v-if="ev.disabled" class="pill pill-off">disabled</span>
      </div>

      <!-- Override section -->
      <div :class="['ev-override', overrideActive ? 'is-active' : '']">

        <div class="ev-override-header">
          <span class="ev-override-title">Override</span>
          <span v-if="overrideActive" class="pill pill-warn">active</span>
        </div>

        <div class="ev-inputs">
          <div class="ev-input-group">
            <label>SoC %</label>
            <input type="number" min="0" max="100" step="5" v-model.number="inputSoc" :disabled="busy" />
          </div>
          <div class="ev-input-group">
            <label>Date</label>
            <input type="date" v-model="inputDate" :disabled="busy" />
          </div>
          <div class="ev-input-group">
            <label>Time</label>
            <input type="time" v-model="inputTime" :disabled="busy" />
          </div>
        </div>

        <div :class="['ev-staged-summary', stagedSummary ? 'has-value' : '']">
          {{ stagedSummary ?? '—' }}
        </div>

        <div class="ev-override-footer">
          <button
            class="btn-save"
            :disabled="!stageDirty || busy"
            @click="onStage"
            title="Save without activating override"
          >Save staged</button>

          <label :class="['toggle-wrap', overrideActive ? 'is-on' : '', (busy || ev.disabled) ? 'is-disabled' : '']" @click.prevent="onToggleOverride">
            <span class="toggle-track">
              <span class="toggle-thumb"></span>
            </span>
            <span class="toggle-label">{{ overrideActive ? 'Override ON' : 'Override OFF' }}</span>
          </label>
        </div>

      </div>

      <!-- Chargepoint enable/disable -->
      <div class="ev-chargepoint-actions">
        <button v-if="ev.disabled" class="btn btn-success" :disabled="busy" @click="onEnable">Enable chargepoint</button>
        <button v-else class="btn btn-danger" :disabled="busy" @click="onDisable">Disable chargepoint</button>
      </div>

      <div v-if="errMsg" class="ev-error">{{ errMsg }}</div>
    </div>
  `,
})
