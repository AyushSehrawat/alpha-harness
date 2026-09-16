import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { llm } from '@/screens/ai/api'

export const useKeys = (refetchInterval: number | false = false) =>
  useQuery({ queryKey: ['ai', 'keys'], queryFn: llm.keys, refetchInterval })
export const useProviders = () =>
  useQuery({
    queryKey: ['ai', 'providers'],
    queryFn: llm.providers,
    staleTime: 10 * 60 * 1000,
  })
export const useModels = () => useQuery({ queryKey: ['ai', 'models'], queryFn: llm.models })

export function useInvalidateKeys() {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: ['ai', 'keys'] })
    void queryClient.invalidateQueries({ queryKey: ['today'] })
  }
}

export function useProviderLabel() {
  const providers = useProviders()
  return (id: string) => providers.data?.providers.find((p) => p.id === id)?.label ?? id
}

/** Seconds left on a server countdown, ticking locally between refetches. */
export function useCountdown(seconds: number | undefined, fetchedAt: number): number | undefined {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 15_000)
    return () => clearInterval(id)
  }, [])
  return seconds == null ? undefined : Math.max(0, seconds - (now - fetchedAt) / 1000)
}
