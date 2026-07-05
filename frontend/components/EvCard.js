import { defineComponent, ref, computed } from 'vue'
import { setEvTarget, clearEvTarget, disableChargepoint, enableChargepoint } from '/ui/api.js'

function fmt(v, dec = 0) {
  return v == null ? '—' : Number(v).toFixed(dec)
}

function defaultBy() {
  const d = new Date()
  d.setDate(d.getDate() + 1)
  d.setHours(7, 30, 0, 0)
  return d.toISOString().slice(0, 16)
}

export default defineComponent({
  name: 'EvCard',
  props: { ev: { type: Object, required: true } },
  emits: ['refresh'],

  setup(props, { emit }) {
    const targetSoc = computed(() =>
      props.ev.goal?.target_soc_pct ?? props.ev.charge_limit_soc_pct
    )
    const inputSoc = ref(targetSoc.value)
    const inputBy  = ref(defaultBy())
    const busy     = ref(false)
    const errMsg   = ref(null)

    const socColor = computed(() => {
      const soc = props.ev.soc_pct
      if (soc == null) return 'var(--accent)'
      if (soc >= targetSoc.value) return 'var(--ok)'
      if (soc < 30) return 'var(--bad)'
      return 'var(--accent)'
    })

    async function withBusy(fn) {
      busy.value   = true
      errMsg.value = null
      try {
        await fn()
        emit('refresh')
        await new Promise(r => setTimeout(r, 3000))
        emit('refresh')
      } catch (e) {
        errMsg.value = e.message
      } finally {
        busy.value = false
      }
    }

    const onSet     = () => withBusy(() => setEvTarget(props.ev.asset_id, inputSoc.value, new Date(inputBy.value)))
    const onClear   = () => withBusy(() => clearEvTarget(props.ev.asset_id))
    const onDisable = () => withBusy(() => disableChargepoint(props.ev.asset_id))
    const onEnable  = () => withBusy(() => enableChargepoint(props.ev.asset_id))

    return { inputSoc, inputBy, busy, errMsg, socColor, targetSoc, fmt, onSet, onClear, onDisable, onEnable }
  },

  template: `
    <div class="ev-card">
      <div class="ev-card-header">
        <span class="ev-label">
          {{ ev.label }}
          <span class="ev-device-id">{{ ev.device_id }}</span>
        </span>
        <span :class="['pill', ev.connected ? 'pill-ok' : 'pill-off']">
          {{ ev.connected ? 'Connected' : 'Disconnected' }}
        </span>
      </div>

      <div class="ev-soc-row">
        <span class="ev-soc-num">{{ ev.soc_pct != null ? fmt(ev.soc_pct) + '%' : '—' }}</span>
        <div class="ev-soc-bar">
          <div v-if="ev.soc_pct != null" class="ev-soc-fill"
            :style="{ width: Math.min(ev.soc_pct, 100) + '%', background: socColor }">
          </div>
        </div>
        <span class="ev-soc-limit">limit {{ fmt(ev.charge_limit_soc_pct) }}%</span>
      </div>

      <div class="ev-goal">
        <template v-if="ev.goal">
          Target <strong>{{ fmt(ev.goal.target_soc_pct) }}%</strong>
          by <strong>{{ new Date(ev.goal.target_by).toLocaleString() }}</strong>
        </template>
        <template v-else>No active goal — PV-only charging</template>
        <span v-if="ev.override" class="pill pill-warn">override</span>
        <span v-if="ev.disabled" class="pill pill-off">disabled</span>
      </div>

      <div class="ev-sep"></div>

      <div class="ev-inputs">
        <div class="ev-input-group">
          <label>Target SoC %</label>
          <input type="number" min="0" max="100" step="1" v-model.number="inputSoc" />
        </div>
        <div class="ev-input-group">
          <label>Charge by</label>
          <input type="datetime-local" v-model="inputBy" />
        </div>
      </div>

      <div class="ev-actions">
        <button class="btn btn-primary" :disabled="ev.disabled || busy" @click="onSet">
          Set target
        </button>
        <button v-if="ev.override" class="btn btn-icon btn-neutral" :disabled="busy" @click="onClear" title="Clear override">
          ✕
        </button>
        <button v-if="ev.disabled" class="btn btn-icon btn-success" :disabled="busy" @click="onEnable" title="Enable chargepoint">
          ▶
        </button>
        <button v-else class="btn btn-icon btn-danger" :disabled="busy" @click="onDisable" title="Disable chargepoint">
          ⏸
        </button>
      </div>

      <div v-if="errMsg" class="ev-error">{{ errMsg }}</div>
    </div>
  `,
})
