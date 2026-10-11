import { useEffect, useState } from 'react'
import { api, ApiError } from '@/lib/api'
import {
  buildProviderOptions,
  normalizeProvider,
} from '@/lib/aiProviders'
import {
  resetProviderCatalogCache,
  useProviderCatalog,
} from '@/lib/useProviderOptions'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Card, CardContent } from '@/components/ui/card'
import { ModelCombobox } from '@/components/shared/ModelCombobox'

type FreePair = { provider: string; model: string }

/**
 * Admin management of the free provider+model pair list.
 *
 * Lives on the Server Settings page (AI category) next to the
 * ENABLE_FREE_MODELS toggle it depends on. The pickers draw from the
 * force-server catalog — the same catalog the PUT endpoint validates
 * against — so every offered model is actually saveable. A successful
 * save resets the shared provider catalog cache so model pickers
 * elsewhere pick up new/removed `free` flags immediately.
 */
export function FreeModelsCard() {
  // forceServer=true: the pair list runs on server credentials, so the
  // picker must offer the server catalog, not the admin's personal-key
  // scope (personal-only models would fail PUT validation with a 400).
  const { providers: catalog } = useProviderCatalog(true)
  const [freeModels, setFreeModels] = useState<FreePair[]>([])
  // List fingerprint from the last read; sent back on save so a
  // concurrent admin's change is rejected (409) instead of erased.
  const [freeModelsVersion, setFreeModelsVersion] = useState('')
  const [freeModelsError, setFreeModelsError] = useState('')
  // False until the list GET succeeds. Adding or removing before the list
  // is known would replace the whole list built from an empty snapshot —
  // with no version to guard it, one Add could wipe every declared pair.
  const [listLoaded, setListLoaded] = useState(false)
  // Bumped by the Retry button: a transient load failure must not lock
  // the editor until the page is remounted.
  const [loadAttempt, setLoadAttempt] = useState(0)
  const [freeModelsBusy, setFreeModelsBusy] = useState(false)
  const [freeProvider, setFreeProvider] = useState('')
  const [freeModel, setFreeModel] = useState('')

  useEffect(() => {
    let ignore = false
    api
      .get<{ models: FreePair[]; version?: string }>('/api/admin/free-models')
      .then((res) => {
        if (!ignore) {
          setFreeModels(res.models ?? [])
          setFreeModelsVersion(res.version ?? '')
          setListLoaded(true)
          setFreeModelsError('')
        }
      })
      .catch(() => {
        if (!ignore) {
          setListLoaded(false)
          setFreeModelsError('Failed to load free models')
        }
      })
    return () => {
      ignore = true
    }
  }, [loadAttempt])

  async function saveFreeModels(next: FreePair[]) {
    setFreeModelsBusy(true)
    setFreeModelsError('')
    try {
      const result = await api.put<{ models: FreePair[]; version?: string }>(
        '/api/admin/free-models',
        { models: next, expected_version: freeModelsVersion || undefined },
      )
      setFreeModels(result.models ?? [])
      setFreeModelsVersion(result.version ?? '')
      // Free flags in every model picker come from the shared catalog
      // cache; a changed pair list must invalidate it immediately.
      resetProviderCatalogCache()
      return true
    } catch (err) {
      const detail =
        err instanceof ApiError
          ? (err.body as
              | { detail?: { unknown?: FreePair[]; models?: FreePair[]; version?: string } | string }
              | undefined)?.detail
          : null
      if (
        err instanceof ApiError &&
        err.status === 409 &&
        detail &&
        typeof detail !== 'string'
      ) {
        // Another admin changed the list first: adopt their state so the
        // next save is based on reality, and tell the admin to re-apply.
        setFreeModels(detail.models ?? [])
        setFreeModelsVersion(detail.version ?? '')
        setListLoaded(true)
        resetProviderCatalogCache()
        setFreeModelsError(
          'Free models were changed by someone else — showing their list, please re-apply your change',
        )
      } else if (detail && typeof detail !== 'string' && detail.unknown?.length) {
        setFreeModelsError(
          `Not in catalog: ${detail.unknown
            .map((p) => `${p.provider}/${p.model}`)
            .join(', ')}`,
        )
      } else {
        setFreeModelsError(
          typeof detail === 'string' ? detail : 'Failed to save free models',
        )
      }
      return false
    } finally {
      setFreeModelsBusy(false)
    }
  }

  async function handleAddFreeModel() {
    if (!freeProvider || !freeModel.trim()) return
    const next = [...freeModels, { provider: freeProvider, model: freeModel.trim() }]
    const ok = await saveFreeModels(next)
    // Keep the picks on failure so the admin can retry after an error.
    if (ok) {
      setFreeProvider('')
      setFreeModel('')
    }
  }

  async function handleRemoveFreeModel(provider: string, model: string) {
    await saveFreeModels(
      freeModels.filter((p) => !(p.provider === provider && p.model === model)),
    )
  }

  return (
    <Card className="mt-3 border-border-muted">
      <CardContent className="space-y-4 p-5">
        <div>
          <h3 className="font-display text-sm font-semibold text-text-primary">Free AI models</h3>
          <p className="mt-1 text-xs text-text-tertiary">
            Provider+model pairs every user may run on server credentials while the{' '}
            <span className="font-medium">Enable free models</span> setting is on. &quot;Free&quot; is
            this list — never the catalog&apos;s cost fields.
          </p>
        </div>
        {freeModelsError && (
          <p role="alert" className="text-xs text-signal-red">{freeModelsError}</p>
        )}
        {!listLoaded && freeModelsError ? (
          <div className="flex items-center gap-2">
            <p className="text-sm text-text-tertiary">
              List not loaded — Add and Remove stay disabled until it loads.
            </p>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setLoadAttempt((n) => n + 1)}
            >
              Retry
            </Button>
          </div>
        ) : freeModels.length > 0 ? (
          <ul
            aria-label="Free AI model pairs"
            className="divide-y divide-border-muted border-t border-border-muted"
          >
            {freeModels.map((pair) => (
              <li
                key={`${pair.provider}/${pair.model}`}
                className="flex flex-wrap items-center gap-2 py-2.5"
              >
                <span className="mr-auto font-mono text-sm text-text-primary break-all">
                  {pair.provider} / {pair.model}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={freeModelsBusy || !listLoaded}
                  onClick={() => handleRemoveFreeModel(pair.provider, pair.model)}
                  aria-label={`Remove ${pair.provider}/${pair.model} from free models`}
                >
                  Remove
                </Button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-text-tertiary">No free models declared.</p>
        )}
        <div className="grid gap-3 border-t border-border-muted pt-4 sm:grid-cols-[minmax(0,1fr)_minmax(0,2fr)_auto]">
          <Select
            value={freeProvider || undefined}
            onValueChange={(value) => {
              setFreeProvider(value)
              setFreeModel('')
            }}
          >
            <SelectTrigger aria-label="Free model provider">
              <SelectValue placeholder="Provider..." />
            </SelectTrigger>
            <SelectContent>
              {buildProviderOptions(Object.keys(catalog)).map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <ModelCombobox
            value={freeModel}
            onChange={setFreeModel}
            options={catalog[normalizeProvider(freeProvider)] ?? []}
            ariaLabel="Free model"
            placeholder="Select model..."
            disabled={!freeProvider}
          />
          <Button
            size="sm"
            onClick={handleAddFreeModel}
            disabled={freeModelsBusy || !listLoaded || !freeProvider || !freeModel.trim()}
          >
            Add
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
