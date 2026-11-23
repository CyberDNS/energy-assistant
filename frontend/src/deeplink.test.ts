import { beforeEach, describe, expect, it, vi } from 'vitest'

// deeplink.js reads the page URL when it is loaded, so each case sets the URL
// first and then loads a fresh copy of the module.
async function load(url: string) {
  window.history.replaceState(null, '', url)
  vi.resetModules()
  return await import('./deeplink.js')
}

describe('deep links', () => {
  beforeEach(() => localStorage.clear())

  it('turns ?view= and ?vehicle= into a route', async () => {
    let d = await load('/?view=savings')
    d.applyInitialRoute()
    expect(window.location.hash).toBe('#/savings')

    d = await load('/?vehicle=Banzert&embed=1')
    d.applyInitialRoute()
    expect(window.location.hash).toBe('#/cars/Banzert')
    expect(d.embedFromSearch).toBe(true)
  })

  it('still understands the names of the old tabs', async () => {
    expect((await load('/?view=live')).routeFromSearch()).toBe('/')
    expect((await load('/?view=config')).routeFromSearch()).toBe('/settings')
  })

  it('leaves an explicit hash route alone', async () => {
    const d = await load('/?view=savings#/plan')
    d.applyInitialRoute()
    expect(window.location.hash).toBe('#/plan')
  })

  it('restores the page used last, by its top-level page', async () => {
    let d = await load('/')
    d.rememberPage('/settings/tariffs')
    d = await load('/')
    d.applyInitialRoute()
    expect(window.location.hash).toBe('#/settings')
  })

  it('maps the remembered Config tab of the old UI to Settings', async () => {
    localStorage.setItem('energy-assistant.lastTab', '/config')
    const d = await load('/')
    d.applyInitialRoute()
    expect(window.location.hash).toBe('#/settings')
  })

  it('never restores a page into an embedded view', async () => {
    localStorage.setItem('energy-assistant.lastTab', '/plan')
    const d = await load('/?embed=1')
    d.applyInitialRoute()
    expect(window.location.hash).toBe('')
  })
})
