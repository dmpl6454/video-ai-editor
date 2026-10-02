// The asset browser (design §2a; brief §2 "Asset navigation"): a strip of
// eleven icon-over-label tabs in the reference order, a 118 px sub-nav column
// whose rows come from lib/assetTabs.SUBNAV, and the content for the chosen
// pair. The old left rail's chords (⌥1…⌥8) and deep links drive the same
// store (RAIL_TO_TAB), so nothing that opened a panel before stopped working.
import { useEffect, type KeyboardEvent, type ReactNode } from 'react'
import { useLayoutStore } from '../../lib/layoutStore'
import { useViewStore } from '../../lib/viewStore'
import {
  ASSET_TABS, DEFAULT_SUB, RAIL_TO_TAB, SUBNAV, TAB_ICONS, asAssetTab, assetPanelId, assetTabId, useAssetBrowser, type AssetTab,
} from '../../lib/assetTabs'
import { Icon } from '../Icon'
import { MediaTab } from './MediaTab'
import { AudioTab } from './AudioTab'
import { TextTab } from './TextTab'
import { StickersTab } from './StickersTab'
import { EffectsTab } from './EffectsTab'
import { TransitionsTab } from './TransitionsTab'
import { CaptionsTab } from './CaptionsTab'
import { FiltersTab } from './FiltersTab'
import { AdjustmentTab } from './AdjustmentTab'
import { TemplatesTab } from './TemplatesTab'
import { AiAvatarsTab } from './AiAvatarsTab'
import './assets.css'

function content(tab: AssetTab, sub: string): ReactNode {
  switch (tab) {
    case 'Media': return <MediaTab sub={sub} />
    case 'Audio': return <AudioTab sub={sub} />
    case 'Text': return <TextTab sub={sub} />
    case 'Stickers': return <StickersTab sub={sub} />
    case 'Effects': return <EffectsTab sub={sub} />
    case 'Transitions': return <TransitionsTab sub={sub} />
    case 'Captions': return <CaptionsTab sub={sub} />
    case 'Filters': return <FiltersTab sub={sub} />
    case 'Adjustment': return <AdjustmentTab sub={sub} />
    case 'Templates': return <TemplatesTab sub={sub} />
    case 'AI avatars': return <AiAvatarsTab sub={sub} />
  }
}

export function AssetBrowser() {
  const tab = useAssetBrowser((s) => s.tab)
  const subs = useAssetBrowser((s) => s.subs)
  const setTab = useAssetBrowser((s) => s.setTab)
  const setSub = useAssetBrowser((s) => s.setSub)
  const sub = subs[tab] ?? DEFAULT_SUB[tab] ?? SUBNAV[tab][0]
  // The Audio panel hosts the voiceover recorder, a live process whose take
  // must stay stoppable from the top bar's activity chip with another tab
  // showing (LEFT_RAIL_SPEC §2.8): it stays mounted, hidden, like the old
  // rail kept every panel mounted (§2.7). The other panels mount on demand.
  const audioSub = subs.Audio ?? SUBNAV.Audio[0]

  // The rail's panel chords and deep links (lib/layoutStore) land here.
  const leftTab = useLayoutStore((s) => s.leftTab)
  const aiJump = useLayoutStore((s) => s.aiJump)
  useEffect(() => {
    const m = RAIL_TO_TAB[leftTab]
    if (m) setTab(m.tab, m.sub)
    // Only on a rail change (the strip itself writes the asset store).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [leftTab])
  useEffect(() => {
    if (aiJump) setTab('Media', 'AI media')
  }, [aiJump, setTab])
  // Home › Templates opens the editor on that tab.
  const consumeOpenWith = useViewStore((s) => s.consumeOpenWith)
  useEffect(() => {
    const want = asAssetTab(consumeOpenWith())
    if (want) setTab(want)
  }, [consumeOpenWith, setTab])

  const onTabKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return
    e.preventDefault()
    const i = ASSET_TABS.indexOf(tab)
    const n = ASSET_TABS.length
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? n - 1 : e.key === 'ArrowRight' ? (i + 1) % n : (i - 1 + n) % n
    setTab(ASSET_TABS[next])
    document.getElementById(assetTabId(ASSET_TABS[next]))?.focus()
  }

  return (
    <div className="ab">
      <div className="ab-tabs" role="tablist" aria-label="Assets" onKeyDown={onTabKey}>
        {ASSET_TABS.map((t) => (
          <button key={t} type="button" role="tab" id={assetTabId(t)} aria-selected={tab === t} aria-controls={assetPanelId(t)}
                  tabIndex={tab === t ? 0 : -1} className={`ab-tab${tab === t ? ' is-active' : ''}`} title={t}
                  // A mouse click switches the tab without taking focus, so Space
                  // still plays and N still snaps afterwards (review RD1); the
                  // keyboard reaches the strip through its roving tabindex.
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => setTab(t)}>
            <Icon name={TAB_ICONS[t]} /><span className="ab-tab-label">{t}</span>
          </button>
        ))}
      </div>
      <div className="ab-body">
        <nav className="ab-subnav" aria-label={`${tab} sections`}>
          {SUBNAV[tab].map((s) => (
            <button key={s} type="button" className={`ab-sub${sub === s ? ' is-active' : ''}`} aria-current={sub === s ? 'true' : undefined}
                    onClick={() => setSub(s)}>{s}</button>
          ))}
        </nav>
        <div className="ab-content" role="tabpanel" id={assetPanelId('Audio')} aria-labelledby={assetTabId('Audio')} data-panel="Audio"
             data-sub={audioSub} tabIndex={-1} hidden={tab !== 'Audio'}>
          <AudioTab sub={audioSub} />
        </div>
        {tab !== 'Audio' && (
          <div className="ab-content" role="tabpanel" id={assetPanelId(tab)} aria-labelledby={assetTabId(tab)} data-panel={tab} data-sub={sub} tabIndex={-1}>
            {content(tab, sub)}
          </div>
        )}
      </div>
    </div>
  )
}
