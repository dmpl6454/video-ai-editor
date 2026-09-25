import { describe, expect, it } from 'vitest'
import { initialFocusIndex, menuKeyIntent } from './menuNav'

describe('menuKeyIntent — menu', () => {
  it('arrows wrap around the items', () => {
    expect(menuKeyIntent('ArrowDown', 'menu', 0, 3)).toEqual({ kind: 'focus', index: 1 })
    expect(menuKeyIntent('ArrowDown', 'menu', 2, 3)).toEqual({ kind: 'focus', index: 0 })
    expect(menuKeyIntent('ArrowUp', 'menu', 0, 3)).toEqual({ kind: 'focus', index: 2 })
  })
  it('an arrow with nothing focused enters at the matching end', () => {
    expect(menuKeyIntent('ArrowDown', 'menu', -1, 4)).toEqual({ kind: 'focus', index: 0 })
    expect(menuKeyIntent('ArrowUp', 'menu', -1, 4)).toEqual({ kind: 'focus', index: 3 })
  })
  it('Home / End jump to the ends', () => {
    expect(menuKeyIntent('Home', 'menu', 2, 5)).toEqual({ kind: 'focus', index: 0 })
    expect(menuKeyIntent('End', 'menu', 0, 5)).toEqual({ kind: 'focus', index: 4 })
  })
  it('Escape and Tab close a menu', () => {
    expect(menuKeyIntent('Escape', 'menu', 1, 3)).toEqual({ kind: 'close' })
    expect(menuKeyIntent('Tab', 'menu', 1, 3)).toEqual({ kind: 'close' })
  })
  it('Escape closes even an empty menu (the picker while it loads)', () => {
    expect(menuKeyIntent('Escape', 'menu', -1, 0)).toEqual({ kind: 'close' })
  })
  it('other keys are left alone (Enter / Space activate the focused button natively)', () => {
    expect(menuKeyIntent('Enter', 'menu', 0, 3)).toEqual({ kind: 'none' })
    expect(menuKeyIntent(' ', 'menu', 0, 3)).toEqual({ kind: 'none' })
  })
})

describe('menuKeyIntent — dialog', () => {
  it('Tab past the last control wraps to the first; Shift+Tab before the first wraps to the last', () => {
    expect(menuKeyIntent('Tab', 'dialog', 3, 4)).toEqual({ kind: 'focus', index: 0 })
    expect(menuKeyIntent('Tab', 'dialog', 0, 4, true)).toEqual({ kind: 'focus', index: 3 })
  })
  it('an interior Tab is the browser\'s', () => {
    expect(menuKeyIntent('Tab', 'dialog', 1, 4)).toEqual({ kind: 'none' })
    expect(menuKeyIntent('Tab', 'dialog', 2, 4, true)).toEqual({ kind: 'none' })
  })
  it('focus that escaped the dialog is pulled back in', () => {
    expect(menuKeyIntent('Tab', 'dialog', -1, 4)).toEqual({ kind: 'focus', index: 0 })
    expect(menuKeyIntent('Tab', 'dialog', -1, 4, true)).toEqual({ kind: 'focus', index: 3 })
  })
  it('arrows belong to the <select>s, Escape closes', () => {
    expect(menuKeyIntent('ArrowDown', 'dialog', 0, 4)).toEqual({ kind: 'none' })
    expect(menuKeyIntent('Escape', 'dialog', 0, 4)).toEqual({ kind: 'close' })
  })
})

describe('initialFocusIndex', () => {
  it('prefers the checked item, else the first, else nothing', () => {
    expect(initialFocusIndex([false, true, false])).toBe(1)
    expect(initialFocusIndex([false, false])).toBe(0)
    expect(initialFocusIndex([])).toBe(-1)
  })
})
