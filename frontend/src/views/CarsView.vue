<script setup lang="ts">
import { RouterLink } from 'vue-router'
import EvCard from '../components/EvCard.vue'
import GuestCard from '../components/GuestCard.vue'
import { useCars } from '../composables/useCars'

// Every car: where it charges, its state of charge and its charging targets.
const { cars, chargers, loaded, refresh } = useCars()
</script>

<template>
  <div>
    <div v-if="cars.length" class="ev-cards">
      <EvCard
        v-for="ev in cars" :key="ev.vehicle_id" :ev="ev" :chargers="chargers" collapsible
        :to="'/cars/' + encodeURIComponent(ev.vehicle_id)" @refresh="refresh"
      />
    </div>
    <div v-else-if="loaded" class="panel empty-state">
      <h2>No cars yet</h2>
      <p>Add a vehicle and the charger it uses to plan its charging around prices and PV.</p>
      <RouterLink class="btn btn-primary" to="/settings/devices">Open device settings</RouterLink>
    </div>
    <GuestCard v-if="chargers.length" :chargers="chargers" class="mt-3" @refresh="refresh" />
  </div>
</template>
