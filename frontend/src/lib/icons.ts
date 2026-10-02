// THE app's icon set — the name map components/Icon.tsx renders: lucide-react
// (pinned in package.json), drawn at 16 px in `currentColor`, so the surrounding text token colours every icon — no emoji
// and no palette of their own (QA-125). Every icon in the product goes through
// this one name map, so a surface can never pick up a second style: the
// hand-drawn 16-unit paths that lived here in wave B, the colour emoji before
// that (💾 📂 🎵 ✨ 🔪 📱 ⚠️) and the typographic stand-ins (▾ ▶ × ✕ ⋯ ‹ ›) are
// all gone. Emoji remain only where they ARE the content (the sticker picker).
//
// Decorative by default (aria-hidden): the control that holds an icon carries
// the accessible name. Pass `label` only for an icon that stands alone as
// information (a status mark with no text next to it).

import {
  TriangleAlert, CircleQuestionMark, Aperture, AudioLines, Blend, Camera, CassetteTape, Check, ChevronDown, ChevronLeft,
  ChevronRight, ChevronUp, CircleCheck, CircleX, Copy, CornerDownLeft, Diamond, Download, Droplets,
  Ellipsis, Expand, Film, Focus, FolderOpen, Grip, Image, Info, Keyboard, ListPlus, LoaderCircle, Lock, Magnet, Mic, Minus,
  MoveHorizontal, MoveVertical, Music, Pause, Play, Plus, Redo2, RotateCcw, Save, Scissors, Smartphone,
  Sparkles, Star, Sticker, Sun, Trash, Type, Undo2, Upload, X, Zap, ZoomIn, ZoomOut, Captions,
  Settings, KeyRound, HardDrive, Brain, RefreshCw, Pencil, TextAlignStart, TextAlignCenter, TextAlignEnd,
  Volume2, VolumeX, Headphones,
  SquareSplitHorizontal, WandSparkles, PanelLeftClose, SlidersHorizontal, MessageSquare,
  Square, Clapperboard,
  Snowflake, Spline, Gauge,
  Squirrel, ArrowDownNarrowWide, Skull, Bot, Repeat, Church, Phone, Megaphone, Radio, Waves, AudioWaveform, Ban, MicVocal,
  FlipHorizontal2, FlipVertical2,
  LogIn, LogOut, Sunrise, Sunset, ArrowLeft, ArrowRight, ArrowUp, ArrowDown, RotateCw, Activity, Rotate3d,
  Anchor, Vibrate, Scaling,
  Palette, CopyCheck, Layers,
  // Design handoff 2026-10-02 (the Apple dark-mode shell): home, the asset
  // tab strip, the player footer, the timeline toolbar and the modals.
  House, LayoutGrid, LayoutTemplate, Share2, SkipBack, SkipForward, Maximize2, Crop, Link2, Flag,
  MousePointer2, Search, Funnel, Circle, ChevronsUpDown, User, CloudOff, List, Eye, EyeOff, LockOpen,
  Shuffle, Folder, Pipette, Wand, AlignStartHorizontal, AlignCenterHorizontal, AlignEndHorizontal,
  AlignStartVertical, AlignCenterVertical, AlignEndVertical, CirclePlus, Droplet, ScanFace, StretchHorizontal,
  Film as FilmStrip, Clock, ArrowUpFromLine, Image as ImageIcon, Scissors as ScissorsIcon,
  type LucideIcon,
} from 'lucide-react'

export const ICONS = {
  // Timeline toolbar (QA-053)
  split: Scissors,
  delete: Trash,
  duplicate: Copy,
  snap: Magnet,
  zoomOut: ZoomOut,
  zoomIn: ZoomIn,
  fit: Expand,
  play: Play,
  pause: Pause,
  undo: Undo2,
  redo: Redo2,
  // Top bar / media
  save: Save,
  open: FolderOpen,
  download: Download,
  upload: Upload,
  keyboard: Keyboard,
  help: CircleQuestionMark,
  music: Music,
  mic: Mic,
  audioFile: AudioLines,
  sticker: Sticker,
  effects: Sparkles,
  film: Film,
  image: Image,
  text: Type,
  captions: Captions,
  phone: Smartphone,
  plus: Plus,
  addToTimeline: ListPlus,
  // Status and chrome
  check: Check,
  close: X,
  more: Ellipsis,
  warning: TriangleAlert,
  info: Info,
  ok: CircleCheck,
  fail: CircleX,
  minus: Minus,
  loading: LoaderCircle,
  lock: Lock,
  fast: Zap,
  reset: RotateCcw,
  custom: Star,
  enter: CornerDownLeft,
  chevronDown: ChevronDown,
  chevronUp: ChevronUp,
  chevronLeft: ChevronLeft,
  chevronRight: ChevronRight,
  keyframe: Diamond,
  // Effect presets (EffectsPanel)
  blur: Droplets,
  sharpen: Focus,
  vignette: Aperture,
  grain: Grip,
  vintage: Camera,
  vhs: CassetteTape,
  glow: Sun,
  rgbSplit: Blend,
  flipH: MoveHorizontal,
  flipV: MoveVertical,
  // Settings dialog (QA-063-SETTINGS)
  settings: Settings,
  key: KeyRound,
  storage: HardDrive,
  brain: Brain,
  // Wave C review
  copy: Copy,
  refresh: RefreshCw,
  rename: Pencil,
  alignLeft: TextAlignStart,
  alignCenter: TextAlignCenter,
  alignRight: TextAlignEnd,
  laneAudible: Volume2,
  laneMuted: VolumeX,
  solo: Headphones,
  // Wave D, left tool rail (docs/design/LEFT_RAIL_SPEC.md §2.2, R1)
  transitions: SquareSplitHorizontal,
  ai: WandSparkles,
  panelLeftClose: PanelLeftClose,
  inspector: SlidersHorizontal,
  chat: MessageSquare,
  // R2: the activity chip's Stop recording (§2.8)
  stop: Square,
  // R3: the brand mark over the rail, in the top bar's left group (§3)
  brand: Clapperboard,
  // Wave D S2: Freeze frame, and the Inspector's Speed modes (Normal | Curve)
  freeze: Snowflake,
  speedNormal: Gauge,
  speedCurve: Spline,
  // Wave E F3: the Inspector's Voice effects grid (edl/voice_effects.py names
  // these in its table's `icon`; voiceFx.test.ts pins that every one exists)
  voiceEffects: MicVocal,
  voiceNone: Ban,
  voiceChipmunk: Squirrel,
  voiceDeep: ArrowDownNarrowWide,
  voiceMonster: Skull,
  voiceRobot: Bot,
  voiceEcho: Repeat,
  voiceHall: Church,
  voicePhone: Phone,
  voiceMegaphone: Megaphone,
  voiceRadio: Radio,
  voiceUnderwater: Waves,
  voiceVibrato: AudioWaveform,
  // Wave E F4b: the Inspector's Mirror toggles (Transform flip_h / flip_v).
  // Lucide 1.x draws each by its AXIS: FlipVertical2 is the left-right mirror
  // (a vertical axis, arrows either side) — measured in the screenshot.
  mirrorH: FlipVertical2,
  mirrorV: FlipHorizontal2,
  // Wave E F1: clip animations — the Inspector's In | Out | Combo tabs and
  // each preset's static glyph (shown in place of its looping preview when
  // the viewer asks for reduced motion). Names from edl/clip_animations.py.
  animIn: LogIn,
  animOut: LogOut,
  animCombo: Repeat,
  animFadeIn: Sunrise,
  animFadeOut: Sunset,
  animLeft: ArrowLeft,
  animRight: ArrowRight,
  animUp: ArrowUp,
  animDown: ArrowDown,
  animRotate: RotateCw,
  animSpin: RefreshCw,
  animBounce: Activity,
  animRock: Rotate3d,
  animSwing: MoveHorizontal,
  animPendulum: Anchor,
  animShake: Vibrate,
  animBreathe: Scaling,
  // Canvas background and blend modes (wave E, F2)
  canvasNone: Ban,
  canvasColor: Palette,
  canvasBlur: Droplets,
  canvasImage: Image,
  applyAll: CopyCheck,
  blendMode: Layers,
  // The redesigned shell (design handoff 2026-10-02)
  home: House,
  grid: LayoutGrid,
  layout: LayoutTemplate,
  share: Share2,
  export: ArrowUpFromLine,
  skipBack: SkipBack,
  skipForward: SkipForward,
  fullscreen: Maximize2,
  crop: Crop,
  link: Link2,
  marker: Flag,
  select: MousePointer2,
  search: Search,
  filter: Funnel,
  record: Circle,
  caretUpDown: ChevronsUpDown,
  user: User,
  cloudOff: CloudOff,
  list: List,
  eye: Eye,
  eyeOff: EyeOff,
  lockOpen: LockOpen,
  shuffle: Shuffle,
  folder: Folder,
  eyedropper: Pipette,
  magicWand: Wand,
  alignLeftEdge: AlignStartVertical,
  alignCenterH: AlignCenterVertical,
  alignRightEdge: AlignEndVertical,
  alignTop: AlignStartHorizontal,
  alignCenterV: AlignCenterHorizontal,
  alignBottom: AlignEndHorizontal,
  plusCircle: CirclePlus,
  dropHalf: Droplet,
  avatar: ScanFace,
  previewAxis: StretchHorizontal,
  filmStrip: FilmStrip,
  clock: Clock,
  imageIcon: ImageIcon,
  scissors: ScissorsIcon,
  stack: Layers,
} satisfies Record<string, LucideIcon>

export type IconName = keyof typeof ICONS

/** The one icon size for controls. Illustration-scale icons (an empty state)
 *  pass their own `size`; nothing else should. */
export const ICON_SIZE = 16
/** Lucide's 24-unit stroke of 2 reads heavy at 16 px next to 12 px UI text. */
export const ICON_STROKE = 1.75

// The same geometry for a <canvas> (the timeline's lane-label padlock): the
// lucide component's own icon node, stroked through Path2D, so a canvas-drawn
// cue is the identical glyph rather than a second hand-drawn set.
type IconNode = [string, Record<string, string>][]

export function iconNode(name: IconName): IconNode {
  // A lucide icon is forwardRef((props, ref) => createElement(Icon, { icon }));
  // calling its render with no props hands back that element and its data.
  const C = ICONS[name] as unknown as { render?: (p: object, r: null) => { props?: { icon?: { node?: IconNode } } } }
  return C.render?.({}, null)?.props?.icon?.node ?? []
}

/** `strokePx`: the stroke's width on screen (default lucide's 2 of 24
 *  units, which is under a pixel for a glyph smaller than 12 px — the speed
 *  badge's 8 px glyph asks for 1.4 px so it reads). */
export function drawIcon(ctx: CanvasRenderingContext2D, name: IconName, x: number, y: number, size: number, color: string,
                         strokePx?: number) {
  const node = iconNode(name)
  const k = size / 24
  ctx.save()
  ctx.translate(x, y)
  ctx.scale(k, k)
  ctx.strokeStyle = color
  ctx.lineWidth = strokePx ? strokePx / k : 2
  ctx.lineCap = 'round'
  ctx.lineJoin = 'round'
  for (const [tag, a] of node) {
    const n = (v: string | undefined) => Number(v ?? 0)
    const p = new Path2D()
    if (tag === 'path') p.addPath(new Path2D(a.d))
    else if (tag === 'rect') p.roundRect(n(a.x), n(a.y), n(a.width), n(a.height), n(a.rx))
    else if (tag === 'circle') p.arc(n(a.cx), n(a.cy), n(a.r), 0, Math.PI * 2)
    else if (tag === 'line') { p.moveTo(n(a.x1), n(a.y1)); p.lineTo(n(a.x2), n(a.y2)) }
    else continue
    ctx.stroke(p)
  }
  ctx.restore()
}
