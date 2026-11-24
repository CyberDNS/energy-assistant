<script setup>
import { ChevronRight } from '@lucide/vue'
import { onMounted } from 'vue'
import { RouterLink } from 'vue-router'
import PlotlyChart from '../components/PlotlyChart.js'
import RangeButtons from '../components/RangeButtons.vue'
import { savingsLink } from '../lib/deviceLinks'
import { useSavings } from '../composables/useSavings.js'

// Savings of the whole house against a simulated uncontrolled house.  What
// each car, the batteries and each controlled device contributed is listed
// with a link; the detail charts live on the device pages.
const {
  RANGES, range, pending, loaded, error, load, eur, kwh, buckets, totals, allTime, pct, stored, bucketLabel,
  savingTraces, cumulativeTraces, costTraces, panels, savingTitle, savingLayout, cumulativeLayout, costLayout,
} = useSavings()
onMounted(() => load(range.value))
</script>

<template>
  <div>
    <RangeButtons :ranges="RANGES" :active="pending ?? range" @pick="load" />

    <div v-if="error" class="bad" style="margin-bottom:8px;font-size:.82rem">⚠ {{ error }}</div>

    <div v-if="loaded && !buckets.length" class="panel savings-empty">
      <h2>No savings recorded in this window</h2>
      <p>
        Savings are booked per 15-minute slot by comparing the real house with a
        simulated uncontrolled house on the same load, PV and prices. The first
        numbers appear a few minutes after start.
      </p>
    </div>

    <template v-else-if="totals">
      <div class="kpis savings-kpis">
        <div class="card">
          <div class="k">Saving</div>
          <div class="v" :class="totals.real >= 0 ? 'ok' : 'bad'">{{ eur(totals.real) }}</div>
          <div v-if="pct != null" class="savings-sub">{{ pct.toFixed(1) }} % of uncontrolled cost</div>
          <div v-if="totals.real_with_stored != null" class="savings-sub"
               title="Counting the energy the real house still holds beyond the uncontrolled one, valued at the mean import price of the last 24 h minus what it cost to store">
            incl. stored energy {{ eur(totals.real_with_stored) }}
          </div>
        </div>
        <div class="card">
          <div class="k">Uncontrolled house</div>
          <div class="v">{{ eur(totals.uncontrolled_cost) }}</div>
          <div class="savings-sub">grid bill {{ eur(totals.uncontrolled_bill) }}</div>
        </div>
        <div class="card">
          <div class="k">Real house</div>
          <div class="v">{{ eur(totals.real_cost) }}</div>
          <div class="savings-sub">grid bill {{ eur(totals.real_bill) }}</div>
        </div>
        <div class="card">
          <div class="k">Still stored</div>
          <div class="v">{{ kwh(stored.real.kwh) }}</div>
          <div class="savings-sub">uncontrolled {{ kwh(stored.uncontrolled.kwh) }}</div>
        </div>
        <div v-if="allTime" class="card">
          <div class="k">All time</div>
          <div class="v" :class="allTime.real >= 0 ? 'ok' : 'bad'">{{ eur(allTime.real) }}</div>
          <div class="savings-sub">over {{ (allTime.hours / 24).toFixed(1) }} days</div>
          <div v-if="allTime.real_with_stored != null" class="savings-sub">
            incl. stored energy {{ eur(allTime.real_with_stored) }}
          </div>
        </div>
      </div>

      <div class="panel full">
        <h2>Saving per {{ savingTitle }}</h2>
        <PlotlyChart :traces="savingTraces" :layout="savingLayout" height="280px" />
      </div>

      <div v-if="panels.length" class="panel full" data-testid="savings-breakdown">
        <h2>Where it comes from</h2>
        <div class="link-list">
          <RouterLink v-for="p in panels" :key="p.id" :to="savingsLink(p)" class="link-row">
            <span class="link-row-main">{{ p.label }}</span>
            <span class="link-row-value" :class="p.saving >= 0 ? 'ok' : 'bad'">{{ eur(p.saving, true) }}</span>
            <ChevronRight class="link-row-chevron" />
          </RouterLink>
        </div>
      </div>

      <div class="row2-eq">
        <div class="panel">
          <h2>Cumulative saving</h2>
          <PlotlyChart :traces="cumulativeTraces" :layout="cumulativeLayout" height="260px" />
        </div>
        <div class="panel">
          <h2>Cost per {{ bucketLabel }}: uncontrolled and real</h2>
          <PlotlyChart :traces="costTraces" :layout="costLayout" barmode="group" height="260px" />
        </div>
      </div>

      <p class="savings-note">
        Compared against a simulated uncontrolled house on the same load, PV and prices:
        its batteries charge from PV surplus only and cover the household load, and cars charge
        as late as possible to reach their charge plan. Stored energy is costed when it is used,
        at what it cost to store it (ledger); charging saves nothing by itself, the saving shows
        when the energy replaces grid power. "Incl. stored energy" also counts the energy one
        house still holds beyond the other, valued at the mean import price of the last 24 h
        minus what it cost to store.
      </p>
    </template>
  </div>
</template>
