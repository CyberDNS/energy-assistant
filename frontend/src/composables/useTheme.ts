import { computed, ref } from 'vue'

export type ThemeChoice = 'system' | 'light' | 'dark'

const KEY = 'energy-assistant.theme'

function stored(): ThemeChoice {
  try {
    const value = localStorage.getItem(KEY)
    return value === 'light' || value === 'dark' ? value : 'system'
  } catch {
    return 'system'
  }
}

const media = typeof matchMedia === 'function' ? matchMedia('(prefers-color-scheme: dark)') : null
const systemDark = ref(media?.matches ?? false)
media?.addEventListener?.('change', (e) => {
  systemDark.value = e.matches
  apply()
})

const choice = ref<ThemeChoice>(stored())

/** True while the dark theme is shown; charts watch it to redraw. */
export const isDark = computed(() => choice.value === 'dark' || (choice.value === 'system' && systemDark.value))

function apply(): void {
  document.documentElement.classList.toggle('dark', isDark.value)
}

export function setTheme(value: ThemeChoice): void {
  choice.value = value
  try {
    localStorage.setItem(KEY, value)
  } catch {
    /* storage blocked: applies until the next reload */
  }
  apply()
}

export function useTheme() {
  return { theme: choice, isDark, setTheme }
}

apply()
