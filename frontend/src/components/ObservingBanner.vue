<script setup lang="ts">
import type { Ref } from 'vue'
import type { Status } from '../composables/usePowerFlow'
import { Eye } from '@lucide/vue'
import { computed, inject, onMounted } from 'vue'
import { RouterLink } from 'vue-router'
import { useProjectedSavings } from '../composables/useProjectedSavings'

// Home, while every device Nornwatt can control is only observed
// (issue #134): what it would have saved so far with control.
const status = inject('status') as Ref<Status | null>
const { projected, load } = useProjectedSavings()
onMounted(load)

const modes = computed(() => Object.values(status.value?.control ?? {}))
const allObserved = computed(() => modes.value.length > 0 && modes.value.every(m => m === 'observe'))
const saving = computed(() => projected.value?.all_time.real ?? null)
const eur = (v: number) => `${v < -0.005 ? '−' : ''}${Math.abs(v).toFixed(2)} €`
</script>

<template>
  <section v-if="allObserved" class="panel full observing" data-testid="observing-banner">
    <Eye class="size-5 shrink-0" />
    <div class="min-w-0 flex-1">
      <span class="font-medium">Nornwatt is observing.</span>
      It plans your devices but controls none of them.
      <template v-if="saving != null">
        Projected saving so far: <b data-testid="observing-saving">{{ eur(saving) }}</b> (simulated).
      </template>
      <template v-else>The projected saving shows here after the first quarter of an hour.</template>
    </div>
    <RouterLink to="/devices" class="section-link">Switch control on per device ›</RouterLink>
  </section>
</template>

<style scoped>
.observing { display: flex; flex-wrap: wrap; align-items: center; gap: .75rem; font-size: .875rem; }
</style>
