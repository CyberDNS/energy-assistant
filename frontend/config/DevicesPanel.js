import { defineComponent, computed, ref } from 'vue'
import DeviceForm from './DeviceForm.js'
import { useConfig } from './useConfig.js'

// The devices of config.yaml grouped by category; a row opens the device's form.
export default defineComponent({
  name: 'DevicesPanel',
  components: { DeviceForm },
  emits: ['notice'],
  setup(_, { emit }) {
    const cfg = useConfig()
    const editing = ref(null)   // { device, category } while a form is open

    const all = computed(() => [...cfg.file.value.devices, ...cfg.file.value.imported])
    const name = (id) => all.value.find(d => d.id === id)?.label || id
    const groups = computed(() => cfg.schema.value.categories
      .map(c => ({ ...c, devices: all.value.filter(d => d.category === c.category) }))
      .filter(g => g.devices.length))

    // What a row says about the device's connections.
    function links(d) {
      const out = []
      if (d.category === 'meter' && !d.metered_by) out.push('grid connection')
      if (d.metered_by) out.push(`behind ${name(d.metered_by)}`)
      if (d.controlled_by) out.push(`controlled by ${name(d.controlled_by)}`)
      if (d.included_in) out.push(`included in ${name(d.included_in)}`)
      if (d.params?.default_vehicle) out.push(`default vehicle ${name(d.params.default_vehicle)}`)
      const controls = cfg.file.value.devices.find(o => o.controlled_by === d.id)
      if (controls) out.push(`controls ${name(controls.id)}`)
      if (d.tariff) out.push(`tariff ${d.tariff}`)
      return out
    }
    function open(device, category = null) {
      emit('notice', null)
      editing.value = { device, category }
      window.scrollTo({ top: 0 })
    }
    function done(message) {
      editing.value = null
      emit('notice', message)
    }
    return { cfg, editing, groups, links, open, done }
  },
  template: `
    <DeviceForm v-if="editing" :key="editing.device?.id ?? 'new'" :device="editing.device" :category="editing.category"
                @done="done" @cancel="editing = null" />
    <template v-else>
      <div class="cfg-toolbar">
        <span class="cfg-hint">Everything in the house: meters, PV, batteries, cars, chargers, loads.</span>
        <span class="ev-actions-spacer"></span>
        <button class="btn btn-primary" @click="open(null)">+ Add device</button>
      </div>
      <div v-for="g in groups" :key="g.category" class="panel full">
        <div class="cfg-group-head">
          <h2>{{ g.title }}</h2>
          <span class="cfg-hint">{{ g.description }}</span>
          <span class="ev-actions-spacer"></span>
          <button class="btn btn-neutral cfg-mini" :aria-label="'Add ' + g.title" @click="open(null, g.category)">+ Add</button>
        </div>
        <div class="cfg-list">
          <button v-for="d in g.devices" :key="d.id" type="button" class="cfg-row" @click="open(d)">
            <span class="cfg-row-main">
              <span class="dev-label">{{ d.label || d.id }}</span>
              <span class="ev-device-id">{{ d.id }}</span>
            </span>
            <span class="cfg-row-meta">
              <span class="pill cfg-type">{{ cfg.pluginTitle('device', d.type) }}</span>
              <span v-if="d.imported" class="pill pill-warn">Energy dashboard</span>
              <span v-for="l in links(d)" :key="l" class="cfg-link">{{ l }}</span>
            </span>
            <span class="cfg-row-edit">{{ d.imported ? 'View' : 'Edit' }} ›</span>
          </button>
        </div>
      </div>
      <div v-if="!groups.length" class="panel full" style="color:var(--muted)">No devices yet — start with the meter at the grid connection.</div>
    </template>
  `,
})
