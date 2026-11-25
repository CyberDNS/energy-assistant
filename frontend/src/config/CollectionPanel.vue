<script>
import { defineComponent, computed, ref } from 'vue'
import EntryForm from './EntryForm.vue'
import { useConfig } from './useConfig.js'

// A section whose entries are configured through plugins: list + form.
export default defineComponent({
  name: 'CollectionPanel',
  components: { EntryForm },
  props: { name: { type: String, required: true } },   // 'tariffs' | 'forecasts'
  emits: ['notice'],
  setup(props, { emit }) {
    const cfg = useConfig()
    const collection = computed(() => cfg.schema.value.collections.find(c => c.name === props.name))
    const entries = computed(() => cfg.file.value[props.name])
    const editing = ref(null)   // { entry } while a form is open

    // A few telling settings for the list row.
    function facts(e) {
      const out = []
      const specs = cfg.plugins(collection.value.plugin_kind).find(p => p.type === e.type)?.settings
      for (const [k, v] of Object.entries(e.settings)) {
        if (v == null || typeof v === 'object' || k === 'default') continue
        const label = specs?.[k]?.label ?? k
        out.push(`${label.replace(/ \(.*\)$/, '')}: ${v}`)
        if (out.length === 3) break
      }
      if (props.name === 'tariffs') {
        // Power charges live under "More settings": name them in the row.
        for (const pc of e.settings.power_charges ?? []) {
          if (pc?.type === 'power_threshold') out.push(`Power charge above ${pc.threshold_kw} kW`)
          else if (pc?.type === 'monthly_peak') out.push(`Month peak ${pc.eur_per_kw_month} €/kW`)
        }
        const users = cfg.file.value.devices.filter(d => d.tariff === e.id).length
        if (users) out.push(`${users} device${users === 1 ? '' : 's'}`)
      }
      return out
    }
    function open(entry) { emit('notice', null); editing.value = { entry }; window.scrollTo({ top: 0 }) }
    function done(message) { editing.value = null; emit('notice', message) }
    return { cfg, collection, entries, editing, facts, open, done }
  },
})
</script>

<template>
  <EntryForm v-if="editing" :key="editing.entry?.id ?? 'new'" :collection="collection" :entry="editing.entry"
             @done="done" @cancel="editing = null" />
  <template v-else>
    <div class="cfg-toolbar">
      <span class="cfg-hint">{{ collection.description }}</span>
      <span class="ev-actions-spacer"></span>
      <button class="btn btn-primary" @click="open(null)">+ Add {{ collection.plugin_kind }}</button>
    </div>
    <div class="panel full">
      <div class="cfg-list">
        <button v-for="e in entries" :key="e.id" type="button" class="cfg-row" @click="open(e)">
          <span class="cfg-row-main"><span class="dev-label">{{ e.id }}</span></span>
          <span class="cfg-row-meta">
            <span class="pill cfg-type">{{ cfg.pluginTitle(collection.plugin_kind, e.type) }}</span>
            <span v-if="e.settings.default" class="pill pill-ok">default</span>
            <span v-for="f in facts(e)" :key="f" class="cfg-link">{{ f }}</span>
          </span>
          <span class="cfg-row-edit">Edit ›</span>
        </button>
        <div v-if="!entries.length" class="cfg-hint" style="padding:8px 6px">Nothing configured yet.</div>
      </div>
    </div>
  </template>
</template>
