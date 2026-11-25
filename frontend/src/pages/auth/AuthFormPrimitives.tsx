import type { SsoProvider } from '@/lib/ssoProviders'
import { Button } from '@/components/ui/button'

/**
 * One full-page sign-in link per configured provider.
 *
 * Rendered on both the login and the register form: the provider list is the
 * deployment's, not the form's, and hiding SSO from people who happen to be
 * registering only makes them pick a weaker credential.
 */
export function SsoProviderButtons({ providers, returnTo }: { providers: SsoProvider[]; returnTo: string }) {
  if (providers.length === 0) return null
  return (
    <div className="space-y-2 border-t border-border-muted pt-4">
      {providers.map((provider) => (
        <Button key={provider.name} asChild variant="outline" className="w-full">
          <a href={`/api/auth/sso/${encodeURIComponent(provider.name)}/login?next=${encodeURIComponent(returnTo)}`}>
            Sign in with {provider.display_name}
          </a>
        </Button>
      ))}
    </div>
  )
}

export function WarningBanner({ message, tone = 'amber' }: { message: string; tone?: 'amber' | 'red' }) {
  if (!message) return null
  return (
    <div
      // A refusal must be announced, not just coloured: red is an error the
      // person has to act on, amber is a notice.
      role={tone === 'red' ? 'alert' : undefined}
      className={`rounded-md border p-3 ${tone === 'red' ? 'border-signal-red/30 bg-signal-red/10' : 'border-signal-amber/30 bg-signal-amber/10'}`}
    >
      <p className={`text-xs ${tone === 'red' ? 'text-signal-red' : 'text-signal-amber'}`}>{message}</p>
    </div>
  )
}
