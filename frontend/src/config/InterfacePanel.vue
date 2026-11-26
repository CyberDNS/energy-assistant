<script setup lang="ts">
import type { ThemeChoice } from '../composables/useTheme'
import { Monitor, Moon, Sun } from '@lucide/vue'
import { ref } from 'vue'
import { Switch } from '@/components/ui/switch'
import { useTheme } from '../composables/useTheme'
import { haState, kioskModePreference, setKioskModePreference } from '../lib/homeassistant'

// Settings of the user interface.  Unlike the rest of Settings they are kept
// in this browser, so each phone, tablet and wall display has its own.
const { theme, setTheme } = useTheme()
const THEMES: { id: ThemeChoice, label: string, icon: typeof Sun }[] = [
  { id: 'system', label: 'Like the device', icon: Monitor },
  { id: 'light', label: 'Light', icon: Sun },
  { id: 'dark', label: 'Dark', icon: Moon },
]

const kiosk = ref(kioskModePreference())
function setKiosk(value: boolean) {
  kiosk.value = value
  setKioskModePreference(value)
}
</script>

<template>
  <div class="panel cfg-form">
    <p class="cfg-note">These settings are stored in this browser and apply to this device only.</p>

    <section>
      <h3>Appearance</h3>
      <div class="cfg-field">
        <span class="cfg-name">Theme</span>
        <div class="mt-1.5 inline-flex flex-wrap gap-1 rounded-lg border p-1" role="radiogroup" aria-label="Theme">
          <button
            v-for="t in THEMES" :key="t.id" type="button" role="radio" :aria-checked="theme === t.id"
            class="flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors"
            :class="theme === t.id ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-accent hover:text-foreground'"
            @click="setTheme(t.id)"
          >
            <component :is="t.icon" class="size-4" /> {{ t.label }}
          </button>
        </div>
        <div class="cfg-hint mt-1.5">"Like the device" follows the light or dark mode of the operating system.</div>
      </div>
    </section>

    <section>
      <h3>Home Assistant</h3>
      <label class="flex max-w-2xl items-start gap-3">
        <Switch :model-value="kiosk" class="mt-0.5" data-testid="kiosk-switch" @update:model-value="setKiosk" />
        <span>
          <span class="cfg-name">Use the whole screen in Home Assistant</span>
          <span class="cfg-hint block">
            Opened from the Home Assistant sidebar, Nornwatt hides Home Assistant's own sidebar and header
            and shows its menu instead. The "Home Assistant" button at the bottom of the menu brings Home Assistant's
            sidebar back. Turn this off to keep both side by side.
          </span>
          <span v-if="!haState.inHomeAssistant" class="cfg-hint mt-1 block">
            Not opened inside Home Assistant right now (or Home Assistant is older than 2026.2), so this has no effect here.
          </span>
        </span>
      </label>
    </section>
  </div>
</template>
