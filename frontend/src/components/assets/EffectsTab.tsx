// Effects (design §2a; brief §2): Video effects is the project's effect
// presets (components/EffectsPanel, the real `add_effect` registry); Body
// effects is not part of this build and says so.
import { EffectsPanel } from '../EffectsPanel'
import { CatalogueFrame, Unavailable } from './Catalogue'

export function EffectsTab({ sub }: { sub: string }) {
  if (sub === 'Body effects') {
    return <Unavailable title="Body effects are not available in this build" body="Body-tracked effects need a pose model that this build does not ship. Video effects on the left apply to any clip." />
  }
  return (
    <CatalogueFrame name="effects">
      <div className="ab-wrapped"><EffectsPanel active show="effects" /></div>
    </CatalogueFrame>
  )
}
