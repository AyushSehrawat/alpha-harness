/** Cumulative PnL with the train and test periods in two colours, and drawdown beneath it. */

import {
  BaselineSeries,
  ColorType,
  createChart,
  LineSeries,
  LineStyle,
  TickMarkType,
  type Time,
} from 'lightweight-charts'
import { useEffect, useRef } from 'react'
import { color, theme } from '@/screens/pool/pnl-chart'

/** Drawdown is stated as BRAIN states it: the fall from the peak over half the book. */
const HALF_BOOK = 10_000_000

export const TRAIN_COLOR = 'color-status-running'
export const TEST_COLOR = 'color-status-warning'

const yearOf = (time: Time) =>
  typeof time === 'object' ? String(time.year) : String(time).slice(0, 4)

const money = (price: number) =>
  new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 2 }).format(price)

export function PortfolioChart({
  dates,
  curve,
  testStart,
  label,
}: {
  dates: string[]
  curve: number[]
  testStart: string | null
  label: string
}) {
  const element = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const node = element.current
    if (!node || curve.length < 2) return
    const hairline = color('color-hairline')
    const guide = color('color-ink-tertiary')
    const tag = color('color-surface-4')
    const chart = createChart(node, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: color('color-ink-subtle'),
        fontFamily: theme('font-mono'),
        fontSize: 11,
        attributionLogo: false,
        panes: { separatorColor: hairline, enableResize: false },
      },
      grid: { vertLines: { visible: false }, horzLines: { color: hairline } },
      rightPriceScale: { borderVisible: false },
      // ~2,500 days: the default 0.5px minimum bar spacing would drop the early years.
      timeScale: {
        borderVisible: false,
        minBarSpacing: 0.01,
        // Years only: a lone month label at the edge reads as a gap in the data.
        tickMarkFormatter: (time: Time, type: TickMarkType) =>
          type === TickMarkType.Year ? yearOf(time) : '',
      },
      crosshair: {
        vertLine: { color: guide, style: LineStyle.Dashed, labelBackgroundColor: tag },
        horzLine: { color: guide, style: LineStyle.Dashed, labelBackgroundColor: tag },
      },
      // The whole history always fits; dragging or zooming only pushed it off the edge.
      handleScroll: false,
      handleScale: false,
    })

    const points = dates.map((date, i) => ({ time: date as Time, value: curve[i] ?? 0 }))
    const line = (name: string) =>
      chart.addSeries(LineSeries, {
        color: color(name),
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
        priceFormat: { type: 'custom', formatter: money },
      })
    // The split day belongs to both periods, so the two lines meet rather than leave a gap.
    const train = testStart ? points.filter((p) => String(p.time) <= testStart) : points
    const test = testStart ? points.filter((p) => String(p.time) >= testStart) : []
    const trainSeries = line(TRAIN_COLOR)
    trainSeries.setData(train)
    trainSeries.createPriceLine({
      price: 0,
      color: guide,
      lineStyle: LineStyle.Dotted,
      lineWidth: 1,
      axisLabelVisible: false,
    })
    if (test.length) line(TEST_COLOR).setData(test)

    const loss = 'color-pnl-negative'
    let peak = Number.NEGATIVE_INFINITY
    const underwater = points.map((p) => {
      peak = Math.max(peak, p.value)
      return { time: p.time, value: (p.value - peak) / HALF_BOOK }
    })
    chart
      .addSeries(
        BaselineSeries,
        {
          baseValue: { type: 'price', price: 0 },
          topLineColor: 'transparent',
          topFillColor1: 'transparent',
          topFillColor2: 'transparent',
          bottomLineColor: color(loss),
          bottomFillColor1: color(loss, 0.05),
          bottomFillColor2: color(loss, 0.35),
          lineWidth: 1,
          priceLineVisible: false,
          lastValueVisible: false,
          priceFormat: { type: 'custom', formatter: (p: number) => `${(p * 100).toFixed(2)}%` },
        },
        1,
      )
      .setData(underwater)
    chart.panes()[1]?.setStretchFactor(0.35)
    chart.timeScale().fitContent()
    return () => chart.remove()
  }, [dates, curve, testStart])

  return <div ref={element} role="img" aria-label={label} className="h-[26rem] w-full min-w-0" />
}

export function ChartLegend({ testStart }: { testStart: string | null }) {
  const swatch = (name: string, text: string) => (
    <span className="flex items-center gap-1.5">
      <span className="h-0.5 w-4" style={{ background: `var(--${name})` }} aria-hidden />
      {text}
    </span>
  )
  return (
    <p className="flex flex-wrap items-center gap-4 text-body-compact text-ink-subtle">
      {swatch(TRAIN_COLOR, testStart ? 'Train Period' : 'PnL')}
      {testStart && swatch(TEST_COLOR, 'Test Period')}
      {swatch('color-pnl-negative', 'Drawdown')}
    </p>
  )
}
