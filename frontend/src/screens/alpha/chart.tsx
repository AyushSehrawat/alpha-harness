/**
 * The Alpha's history on one canvas, three ways: its cumulative PnL beside the
 * investability-constrained PnL, how far under water it was each day, and its Sharpe over
 * the trailing year against the submission cutoff. Figures stay neutral; loss is the only hue.
 */

import {
  BaselineSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  LineSeries,
  LineStyle,
  type Time,
} from 'lightweight-charts'
import { useEffect, useRef } from 'react'
import { color, theme } from '@/screens/pool/pnl-chart'
import type { Point } from './analysis'

export type ChartView = 'pnl' | 'underwater' | 'sharpe'

const compact = (price: number) =>
  new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 2 }).format(price)

export function AlphaChart({
  view,
  pnl,
  constrained,
  net,
  underwater,
  sharpe,
  cutoff,
  testStart,
  label,
}: {
  view: ChartView
  pnl: Point[]
  constrained: Point[]
  /** Cumulative PnL after the chosen trading cost; empty when no cost is set. */
  net: Point[]
  underwater: Point[]
  sharpe: Point[]
  /** The Sharpe a submission needs, drawn across the rolling view. */
  cutoff: number | null
  /** First held-out day, from BRAIN: the train years before it are drawn dimmer. */
  testStart: string | null
  label: string
}) {
  const element = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const node = element.current
    if (!node) return
    const hairline = color('color-hairline')
    const guide = color('color-ink-tertiary')
    const chart = createChart(node, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: color('color-ink-subtle'),
        fontFamily: theme('font-mono'),
        fontSize: 11,
        attributionLogo: false,
      },
      grid: { vertLines: { visible: false }, horzLines: { color: hairline } },
      rightPriceScale: { borderVisible: false },
      // Ten years of days is ~2,500 bars; the default 0.5px minimum cannot fit them, and the
      // chart silently drops the early years instead.
      timeScale: { borderVisible: false, minBarSpacing: 0.01 },
      localization: {
        priceFormatter:
          view === 'underwater'
            ? (p: number) => `${(p * 100).toFixed(1)}%`
            : view === 'sharpe'
              ? (p: number) => p.toFixed(2)
              : compact,
      },
      crosshair: {
        vertLine: {
          color: guide,
          style: LineStyle.Dashed,
          labelBackgroundColor: color('color-surface-4'),
        },
        horzLine: {
          color: guide,
          style: LineStyle.Dashed,
          labelBackgroundColor: color('color-surface-4'),
        },
      },
    })
    const at = (points: Point[]) => points.map((p) => ({ time: p.date as Time, value: p.value }))
    const zero = {
      price: 0,
      color: guide,
      lineStyle: LineStyle.Dotted,
      lineWidth: 1 as const,
      axisLabelVisible: false,
    }

    if (view === 'pnl') {
      if (constrained.length > 1) {
        chart
          .addSeries(LineSeries, {
            color: color('color-ink-tertiary'),
            lineWidth: 1,
            lineStyle: LineStyle.Dashed,
            priceLineVisible: false,
            lastValueVisible: false,
            title: 'Investability constrained',
          })
          .setData(at(constrained))
      }
      if (net.length > 1) {
        // Its own dates, not the gross line's: this series comes from the stored daily PnL,
        // which carries the closing days BRAIN counts but exports in no recordset.
        chart
          .addSeries(LineSeries, {
            color: color('color-status-warning'),
            lineWidth: 1,
            priceLineVisible: false,
            lastValueVisible: false,
            title: 'After cost',
          })
          .setData(at(net))
      }
      const ink = color('color-ink')
      const trained = color('color-ink-subtle')
      const series = chart.addSeries(LineSeries, {
        color: ink,
        lineWidth: 2,
        priceLineVisible: false,
        title: 'PnL',
      })
      // Compared, not matched: the boundary BRAIN reports can fall on a day with no trading.
      const held = (p: Point) => testStart === null || p.date >= testStart
      series.setData(
        pnl.map((p) => ({ time: p.date as Time, value: p.value, color: held(p) ? ink : trained })),
      )
      series.createPriceLine(zero)
      const first = testStart === null ? undefined : pnl.find(held)
      if (first) {
        createSeriesMarkers(series, [
          {
            time: first.date as Time,
            position: 'aboveBar',
            shape: 'arrowDown',
            color: guide,
            text: 'Test',
          },
        ])
      }
    } else if (view === 'underwater') {
      const loss = 'color-pnl-negative'
      chart
        .addSeries(BaselineSeries, {
          baseValue: { type: 'price', price: 0 },
          topLineColor: 'transparent',
          topFillColor1: 'transparent',
          topFillColor2: 'transparent',
          bottomLineColor: color(loss),
          bottomFillColor1: color(loss, 0.05),
          bottomFillColor2: color(loss, 0.35),
          lineWidth: 1,
          priceLineVisible: false,
        })
        .setData(at(underwater))
    } else {
      const series = chart.addSeries(LineSeries, {
        color: color('color-ink'),
        lineWidth: 2,
        priceLineVisible: false,
        title: 'Sharpe, trailing year',
      })
      series.setData(at(sharpe))
      series.createPriceLine(zero)
      if (cutoff !== null) {
        series.createPriceLine({
          price: cutoff,
          color: color('color-status-warning'),
          lineStyle: LineStyle.Dashed,
          lineWidth: 1,
          axisLabelVisible: true,
          title: 'Cutoff',
        })
      }
    }
    chart.timeScale().fitContent()
    return () => chart.remove()
  }, [view, pnl, constrained, net, underwater, sharpe, cutoff, testStart])

  return <div ref={element} role="img" aria-label={label} className="h-80 w-full min-w-0" />
}
