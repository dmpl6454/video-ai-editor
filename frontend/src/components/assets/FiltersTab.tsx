// Filters (design §2a; brief §2): the bundled LUT looks and your own .cube
// files, applied to the selected clip (components/EffectsPanel's LUT half).
import { useState } from 'react'
import { EffectsPanel } from '../EffectsPanel'
import { readFavourites } from '../../lib/favourites'
import { CatalogueFrame, PanelState } from './Catalogue'

const FAV_KEY = 'aive.favourites.filters'

export function FiltersTab({ sub }: { sub: string }) {
  const [favs] = useState<string[]>(() => readFavourites(FAV_KEY))
  if (sub === 'Favorites') {
    return favs.length === 0
      ? <PanelState kind="empty" title="No favourite filters yet" body="Favourites are not part of this build yet — every bundled look is one click away under Filters." />
      : <div className="ui-faint">{favs.join(', ')}</div>
  }
  return (
    <CatalogueFrame name="filters">
      <div className="ab-wrapped"><EffectsPanel active show="luts" /></div>
    </CatalogueFrame>
  )
}
