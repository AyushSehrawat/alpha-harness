import '@fontsource-variable/inter'
import '@fontsource-variable/jetbrains-mono'
import './index.css'
import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { Toaster } from 'sonner'
import { ApiError } from '@/api/http'
import { useLive } from '@/lib/live'
import { telemetry } from '@/lib/ws'
import { router } from '@/router'

/** Any call BRAIN answers with a verification demand raises the global banner. */
const noteVerification = (error: unknown) => {
  if (error instanceof ApiError && error.body.verificationUrl)
    useLive.setState({ verificationUrl: error.body.verificationUrl })
}

const queryClient = new QueryClient({
  queryCache: new QueryCache({ onError: noteVerification }),
  mutationCache: new MutationCache({ onError: noteVerification }),
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchOnWindowFocus: false,
      // The backend says whether a failure is worth repeating: a 429 from BRAIN's throttle
      // is, the daily limit is not.
      retry: (count, error) => {
        if (count >= 2) return false
        if (error instanceof ApiError) return error.status === 0 || error.retryable
        return true
      },
    },
  },
})

const root = document.getElementById('root')
if (!root) throw new Error('index.html has no #root element')

telemetry.connect()

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
      <Toaster
        theme="dark"
        position="bottom-right"
        toastOptions={{
          style: {
            background: 'var(--color-surface-3)',
            border: '1px solid var(--color-hairline-strong)',
            color: 'var(--color-ink)',
            borderRadius: 8,
          },
        }}
      />
    </QueryClientProvider>
  </StrictMode>,
)
