// Transitions (design §2a): the project's real catalogue (components/
// TransitionsPanel — 72 looks, every one a rendered xfade), with the cut it
// targets. Favorites are not part of this build and the tab says so.
import { TransitionsPanel } from '../TransitionsPanel'
import { PanelState } from './Catalogue'

export function TransitionsTab({ sub }: { sub: string }) {
  if (sub === 'Favorites') {
    return <PanelState kind="empty" title="No favourite transitions yet" body="Favourites are not part of this build yet — every look is under Transitions, with a live preview on hover." />
  }
  return <div className="ab-wrapped ab-transitions"><TransitionsPanel active /></div>
}
