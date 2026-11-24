<script>
import { defineComponent, computed, ref, watch, nextTick } from 'vue'

let counter = 0
const MAX_SHOWN = 80

// A text field with a searchable list of suggestions underneath.  Typing
// filters the list by every word (in any order) over each item's value,
// label and detail; ↑ ↓ Enter pick, Escape closes.  Free text is allowed:
// what is typed is the value.  Items: { value, label?, detail?, meta? }.
export default defineComponent({
  name: 'Combobox',
  props: {
    modelValue:  { default: undefined },
    items:       { type: Array, default: () => [] },
    placeholder: { type: String, default: '' },
    ariaLabel:   { type: String, default: null },
    emptyText:   { type: String, default: 'No matches' },
  },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    const listId = `combo-${++counter}`
    const text = ref(props.modelValue ?? '')
    const open = ref(false)
    const active = ref(-1)
    const list = ref(null)
    watch(() => props.modelValue, (v) => { if ((v ?? '') !== text.value.trim()) text.value = v ?? '' })

    const matches = computed(() => {
      const q = text.value.trim().toLowerCase()
      // The field shows a chosen value: list everything, so it can be changed.
      const all = !q || props.items.some(i => String(i.value).toLowerCase() === q)
      if (all) return props.items.slice(0, MAX_SHOWN)
      const words = q.split(/\s+/)
      // Best first: most search words that are whole words of the label (the
      // friendly name), every word in the label, the label starting with the
      // first word, the shorter (closer) label, then the list's own order.
      const scored = []
      props.items.forEach((item, i) => {
        const label = String(item.label ?? item.value).toLowerCase()
        const hay = `${item.value} ${label} ${item.detail ?? ''}`.toLowerCase()
        if (!words.every(w => hay.includes(w))) return
        const tokens = new Set(label.split(/[^a-z0-9äöüß]+/))
        const whole = words.filter(w => tokens.has(w)).length
        const score = (words.every(w => label.includes(w)) ? 0 : 2) + (label.startsWith(words[0]) ? 0 : 1)
        scored.push([-whole, score, label.length, i, item])
      })
      scored.sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2] || a[3] - b[3])
      return scored.slice(0, MAX_SHOWN).map(([, , , , item]) => item)
    })
    const more = computed(() => matches.value.length >= MAX_SHOWN)

    function onInput(e) {
      text.value = e.target.value
      open.value = true
      active.value = -1
      emit('update:modelValue', text.value.trim() || undefined)
    }
    function pick(item) {
      text.value = String(item.value)
      open.value = false
      emit('update:modelValue', item.value)
    }
    async function move(step) {
      if (!open.value) { open.value = true; return }
      const n = matches.value.length
      if (!n) return
      active.value = (active.value + step + n) % n
      await nextTick()
      list.value?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' })
    }
    function onEnter(e) {
      if (open.value && matches.value[active.value]) { e.preventDefault(); pick(matches.value[active.value]) }
    }
    return { listId, text, open, active, list, matches, more, onInput, pick, move, onEnter }
  },
})
</script>

<template>
  <div class="cfg-combo" @focusout="open = false">
    <input type="text" :value="text" :placeholder="placeholder" :aria-label="ariaLabel"
           role="combobox" :aria-expanded="open" :aria-controls="listId" aria-autocomplete="list"
           autocapitalize="off" autocomplete="off" spellcheck="false"
           @focus="open = true" @click="open = true" @input="onInput"
           @keydown.down.prevent="move(1)" @keydown.up.prevent="move(-1)"
           @keydown.enter="onEnter" @keydown.esc="open = false" />
    <ul v-if="open && (items.length || text)" :id="listId" ref="list" class="cfg-combo-list" role="listbox">
      <li v-for="(item, i) in matches" :key="item.value" role="option" :aria-selected="i === active"
          :class="{ active: i === active, chosen: String(item.value) === text.trim() }"
          @mousedown.prevent="pick(item)">
        <span class="cfg-combo-main">
          <span class="cfg-combo-label">{{ item.label || item.value }}</span>
          <span v-if="item.meta" class="cfg-combo-meta">{{ item.meta }}</span>
        </span>
        <span v-if="item.detail" class="cfg-combo-detail">{{ item.detail }}</span>
      </li>
      <li v-if="!matches.length" class="cfg-combo-empty">{{ emptyText }}</li>
      <li v-else-if="more" class="cfg-combo-empty">Type to narrow down …</li>
    </ul>
  </div>
</template>
