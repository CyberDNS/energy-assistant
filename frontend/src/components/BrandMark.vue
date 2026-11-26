<script setup lang="ts">
// The Nornwatt mark (branding/nornwatt-mark-*.svg): three threads (past,
// present, future) running into one bolt.  Threads take the text colour, the
// bolt the theme's amber (--brand-bolt), so it fits light and dark alike.
</script>

<template>
  <svg viewBox="0 0 120 120" role="img" aria-label="Nornwatt" fill="none" stroke-linecap="round" stroke-linejoin="round">
    <g stroke="currentColor" stroke-width="7">
      <path d="M24 12 C24 46 60 34 60 60" />
      <path d="M60 12 V60" />
      <path d="M96 12 C96 46 60 34 60 60" />
    </g>
    <path d="M60 60 L41 85 H71 L52 110" stroke="var(--brand-bolt)" stroke-width="10" />
  </svg>
</template>
