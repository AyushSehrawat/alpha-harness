/**
 * The one gate. Nothing works without a BRAIN session.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useState } from 'react'
import { auth } from '@/api/core'
import { ApiError } from '@/api/http'
import { useLive } from '@/lib/live'
import { Button, ErrorNotice, Field, Input } from '@/ui/kit'

/** Larger than the in-app controls: this screen is the whole page and is read from arm's length. */
const FIELD = '[&>span:first-child]:text-body'
const INPUT = 'h-11 px-4 text-title'

/** A missing entry, said under its own field instead of in the notice. */
class FieldError extends Error {
  field: 'email' | 'password'
  constructor(field: 'email' | 'password', message: string) {
    super(message)
    this.field = field
  }
}

export function SignIn({ storedEmail }: { storedEmail: string | null }) {
  const queryClient = useQueryClient()
  const [email, setEmail] = useState(storedEmail ?? '')
  const [password, setPassword] = useState('')
  const [shown, setShown] = useState(false)

  const signIn = useMutation({
    mutationFn: async () => {
      const trimmed = email.trim()
      const reuseStored = !password && storedEmail && (!trimmed || trimmed === storedEmail)
      if (!trimmed && !storedEmail) throw new FieldError('email', 'Enter your BRAIN account email.')
      if (!password && !reuseStored) throw new FieldError('password', 'Enter your password.')
      const session = reuseStored ? await auth.login() : await auth.login(trimmed, password)
      if (!session.authenticated) {
        throw new ApiError(401, {
          code: session.verificationUrl ? 'verification_required' : 'not_authenticated',
          message: session.detail || 'That email and password were not accepted.',
          ...(session.verificationUrl ? { verificationUrl: session.verificationUrl } : {}),
        })
      }
      return session
    },
    onSuccess: () => {
      setPassword('')
      useLive.setState({ verificationUrl: null })
      void queryClient.invalidateQueries()
    },
  })

  const needsVerification =
    signIn.error instanceof ApiError && Boolean(signIn.error.body.verificationUrl)
  const missing = signIn.error instanceof FieldError ? signIn.error : null

  return (
    <div className="flex min-h-svh flex-col items-center justify-center p-6">
      <div className="flex w-full max-w-120 flex-col gap-8">
        <div className="flex items-center gap-3 self-center">
          <span
            className="flex size-9 items-center justify-center rounded-md border border-hairline-strong bg-surface-2"
            aria-hidden
          >
            <span className="text-title font-bold text-primary">α</span>
          </span>
          <span className="text-headline font-semibold text-ink">Alpha Harness</span>
        </div>

        <form
          autoComplete="off"
          className="panel-highlight flex flex-col gap-5 rounded-lg border border-hairline bg-surface-1 p-10"
          onSubmit={(event: FormEvent) => {
            event.preventDefault()
            signIn.mutate()
          }}
        >
          <div className="mb-2 flex flex-col gap-2 text-center">
            <h1 className="text-headline">Sign in to BRAIN</h1>
            <p className="text-title text-ink-subtle">Use your WorldQuant BRAIN account.</p>
          </div>

          <Field
            label="Email"
            className={FIELD}
            error={missing?.field === 'email' && missing.message}
          >
            <Input
              type="email"
              autoComplete="email"
              placeholder="you@example.com"
              required={!storedEmail}
              value={email}
              disabled={signIn.isPending}
              aria-invalid={missing?.field === 'email'}
              className={INPUT}
              onChange={(event) => {
                signIn.reset()
                setEmail(event.target.value)
              }}
            />
          </Field>
          <Field
            // Named by this span alone: the Show button inside the label would otherwise join the name.
            label={<span id="brain-password-name">Password</span>}
            className={FIELD}
            error={missing?.field === 'password' && missing.message}
          >
            <div className="relative">
              <Input
                id="brain-password"
                aria-labelledby="brain-password-name"
                type={shown ? 'text' : 'password'}
                autoComplete="current-password"
                placeholder={
                  storedEmail ? 'Leave blank to use the saved password' : 'Your BRAIN password'
                }
                value={password}
                disabled={signIn.isPending}
                aria-invalid={missing?.field === 'password'}
                className={`${INPUT} pr-18`}
                onChange={(event) => {
                  signIn.reset()
                  setPassword(event.target.value)
                }}
              />
              <button
                type="button"
                aria-pressed={shown}
                aria-controls="brain-password"
                disabled={signIn.isPending}
                onClick={() => setShown((s) => !s)}
                className="absolute inset-y-0 right-3 my-auto h-8 rounded-sm px-2 text-body text-ink-subtle transition-colors hover:bg-surface-2 hover:text-ink disabled:text-ink-disabled"
              >
                {shown ? 'Hide' : 'Show'}
              </button>
            </div>
          </Field>

          {signIn.isError && !missing && (
            <ErrorNotice
              error={signIn.error}
              title={needsVerification ? 'BRAIN needs to verify your identity' : 'Sign-in failed'}
            />
          )}

          <Button
            type="submit"
            variant="primary"
            loading={signIn.isPending}
            className="mt-1 h-11 text-title"
          >
            {signIn.isPending ? 'Signing in…' : 'Sign in'}
          </Button>
        </form>
      </div>
    </div>
  )
}
