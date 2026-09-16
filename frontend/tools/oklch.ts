/**
 * Offline generator for the token system (DESIGN.md → Colour): OKLCH ↔ sRGB, gamut, oklab
 * mixing, APCA, WCAG 2, ΔE_OK and colour-vision simulation.
 *
 * The palette in `src/index.css` was generated here and is pasted in, so nothing at runtime
 * imports this: it lives in `tools/` beside the codegen, and `tsconfig.node.json` type-checks
 * it. Reach for it when adding a hue to the operator ramps or re-checking contrast.
 */

export type Oklch = { l: number; c: number; h: number }
type Triple = [number, number, number]

const rad = Math.PI / 180

function oklabOf({ l, c, h }: Oklch): Triple {
  return [l, c * Math.cos(h * rad), c * Math.sin(h * rad)]
}

function oklchOf([l, a, b]: Triple): Oklch {
  return { l, c: Math.hypot(a, b), h: (((Math.atan2(b, a) / rad) % 360) + 360) % 360 }
}

function linearOf(color: Oklch): Triple {
  const [L, a, b] = oklabOf(color)
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
  const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3
  return [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ]
}

function fromLinear([r, g, b]: Triple): Oklch {
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
  return oklchOf([
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ])
}

const encode = (v: number) => (v <= 0.0031308 ? 12.92 * v : 1.055 * v ** (1 / 2.4) - 0.055)
const clamp = (v: number) => Math.min(1, Math.max(0, v))

/** Gamma-encoded sRGB, unclamped, so an out-of-gamut colour shows up outside 0–1. */
export const srgb = (color: Oklch): Triple => linearOf(color).map(encode) as Triple

export const inGamut = (color: Oklch) => srgb(color).every((v) => v >= -1e-6 && v <= 1 + 1e-6)

export function hex(color: Oklch): string {
  return `#${srgb(color)
    .map((v) =>
      Math.round(clamp(v) * 255)
        .toString(16)
        .padStart(2, '0'),
    )
    .join('')}`
}

/** The largest in-gamut chroma at this lightness and hue. */
export function maxChroma(l: number, h: number): number {
  let [lo, hi] = [0, 0.4]
  for (let i = 0; i < 32; i++) {
    const mid = (lo + hi) / 2
    if (inGamut({ l, c: mid, h })) lo = mid
    else hi = mid
  }
  return lo
}

/** `color-mix(in oklab, a p, b)`: `p` is a's share, 0–1. */
export function mix(a: Oklch, b: Oklch, p: number): Oklch {
  const [x, y] = [oklabOf(a), oklabOf(b)]
  return oklchOf([0, 1, 2].map((i) => (x[i] ?? 0) * p + (y[i] ?? 0) * (1 - p)) as Triple)
}

/** ΔE_OK: Euclidean distance in oklab. 0.02 is about a just-noticeable difference. */
export function deltaE(a: Oklch, b: Oklch): number {
  const [x, y] = [oklabOf(a), oklabOf(b)]
  return Math.hypot((x[0] ?? 0) - (y[0] ?? 0), (x[1] ?? 0) - (y[1] ?? 0), (x[2] ?? 0) - (y[2] ?? 0))
}

/** APCA-W3 0.0.98G lightness contrast, as |Lc|. */
export function apca(text: Oklch, background: Oklch): number {
  const y = (color: Oklch) => {
    const [r, g, b] = srgb(color).map((v) => clamp(v) ** 2.4) as Triple
    const lum = 0.2126729 * r + 0.7151522 * g + 0.072175 * b
    // The black clamp exponent is published as 1.414, i.e. √2; the difference is under Lc 0.01.
    return lum < 0.022 ? lum + (0.022 - lum) ** Math.SQRT2 : lum
  }
  const [yt, yb] = [y(text), y(background)]
  if (Math.abs(yb - yt) < 0.0005) return 0
  const s = yb > yt ? (yb ** 0.56 - yt ** 0.57) * 1.14 : (yb ** 0.65 - yt ** 0.62) * 1.14
  if (Math.abs(s) < 0.1) return 0
  return (Math.abs(s) - 0.027) * 100
}

/** WCAG 2 contrast ratio. */
export function wcag(a: Oklch, b: Oklch): number {
  const lum = (color: Oklch) => {
    const [r, g, b2] = linearOf(color).map(clamp) as Triple
    return 0.2126 * r + 0.7152 * g + 0.0722 * b2
  }
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x) as [number, number]
  return (hi + 0.05) / (lo + 0.05)
}

/** Machado, Oliveira & Fernandes (2009) at full severity, applied to linear sRGB. */
const CVD: Record<'protanopia' | 'deuteranopia', [Triple, Triple, Triple]> = {
  protanopia: [
    [0.152286, 1.052583, -0.204868],
    [0.114503, 0.786281, 0.099216],
    [-0.003882, -0.048116, 1.051998],
  ],
  deuteranopia: [
    [0.367322, 0.860646, -0.227968],
    [0.280085, 0.672501, 0.047413],
    [-0.01182, 0.04294, 0.968881],
  ],
}

export function simulate(color: Oklch, kind: keyof typeof CVD): Oklch {
  const rgb = linearOf(color).map(clamp)
  return fromLinear(
    CVD[kind].map((row) => row.reduce((sum, k, i) => sum + k * (rgb[i] ?? 0), 0)) as Triple,
  )
}

// ── Palette ─────────────────────────────────────────────────────────────────────────────

export const STOPS = [
  '050',
  '100',
  '200',
  '300',
  '400',
  '500',
  '600',
  '700',
  '800',
  '900',
  '950',
] as const
export const LIGHTNESS = [0.98, 0.94, 0.88, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.12] as const
/** Dark surfaces step up in lightness instead of casting shadows; the last two are hairlines. */
export const ELEVATION = [0.12, 0.16, 0.2, 0.23, 0.26, 0.3, 0.35] as const

/** Hue and the chroma a stop at L 0.55 would ask for before the gamut clamp. */
export const HUES: Record<string, [hue: number, peak: number]> = {
  neutral: [262, 0.012],
  lavender: [276, 0.16],
  sea: [185, 0.24],
  red: [24, 0.22],
  amber: [77, 0.2],
  orchid: [322, 0.13],
  teal: [227, 0.12],
  jade: [158, 0.12],
  copper: [58, 0.13],
  olive: [97, 0.12],
  rose: [354, 0.13],
}

const MID = 0.55
const SPAN = 0.43 // MID to either end of the scale
const DRIFT = 8 // degrees at the ends
const COOL = 264 // shadows lean blue
const WARM = 80 // highlights lean amber

/** Rotate `h` toward `target` by at most `max` degrees, the short way round. */
function toward(h: number, target: number, max: number): number {
  const d = ((target - h + 540) % 360) - 180
  return (h + Math.sign(d) * Math.min(Math.abs(d), max) + 360) % 360
}

/**
 * One tone of a hue: chroma follows a bell curve around L 0.55 and stays at 95% of the sRGB
 * boundary, so no stop clips; the hue drifts cooler in shadows and warmer in highlights.
 */
export function tone(l: number, [hue, peak]: [number, number]): Oklch {
  const t = (l - MID) / SPAN
  const h = Number(toward(hue, t < 0 ? COOL : WARM, DRIFT * Math.min(1, Math.abs(t))).toFixed(1))
  const wanted = peak * Math.exp(-(((l - MID) / 0.35) ** 2))
  const c = Math.floor(Math.min(wanted, 0.95 * maxChroma(l, h)) * 1000) / 1000
  return { l, c, h }
}

/** Shortest numerals, as Biome writes them, so the formatted sheet still equals the generator. */
export const css = ({ l, c, h }: Oklch) =>
  `oklch(${Number(l.toFixed(2))} ${Number(c.toFixed(3))} ${Number(h.toFixed(1))})`

/** DESIGN.md colours, verbatim: the spec is the source of truth, so these are not generated. */
export const SPEC: Record<string, string> = {
  primary: '#5e6ad2',
  'on-primary': '#ffffff',
  'primary-hover': '#828fff',
  'primary-focus': '#5e69d1',
  canvas: '#010102',
  'surface-1': '#0f1011',
  'surface-2': '#141516',
  'surface-3': '#18191a',
  'surface-4': '#1f2022',
  hairline: '#23252a',
  'hairline-strong': '#34343a',
  'hairline-subtle': '#17181c',
  ink: '#f7f8f8',
  'ink-muted': '#d0d6e0',
  'ink-subtle': '#8a8f98',
  'ink-tertiary': '#52565e',
  'pnl-positive': '#27a644',
  'pnl-negative': '#e5484d',
  'status-running': '#3b82f6',
  'status-queued': '#8a8f98',
  'status-warning': '#f59e0b',
  'status-idle': '#23252a',
}

/** Every Tier 1 primitive, name → value, in declaration order. */
export function primitives(): [string, string][] {
  const out: [string, string][] = [
    ['white', 'oklch(1 0 0)'],
    ['black', 'oklch(0 0 0)'],
    ...Object.entries(SPEC).map(([name, value]): [string, string] => [`spec-${name}`, value]),
  ]
  ELEVATION.forEach((l, i) => {
    out.push([`neutral-elev-${i}`, css(tone(l, HUES['neutral'] ?? [0, 0]))])
  })
  for (const [name, spec] of Object.entries(HUES)) {
    STOPS.forEach((stop, i) => {
      out.push([`${name}-${stop}`, css(tone(LIGHTNESS[i] ?? 0, spec))])
    })
  }
  return out
}

export function parse(value: string): Oklch {
  const x = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(value.trim())
  if (x) {
    const decode = (v: number) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)
    return fromLinear(
      [x[1], x[2], x[3]].map((h) => decode(Number.parseInt(h ?? '0', 16) / 255)) as Triple,
    )
  }
  const m = /^oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\)$/.exec(value.trim())
  if (!m) throw new Error(`not an oklch() colour: ${value}`)
  return { l: Number(m[1]), c: Number(m[2]), h: Number(m[3]) }
}
