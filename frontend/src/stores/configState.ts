// Bumped whenever the configuration was saved or applied from this UI, so the
// shell (attention banner) and the settings pages re-read what depends on it.
import { ref } from 'vue'

export const configRevision = ref(0)
