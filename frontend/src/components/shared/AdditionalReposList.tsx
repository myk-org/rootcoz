import { Input } from '@/components/ui/input'
import { Plus, Trash2 } from 'lucide-react'

export type RepoWithId = { id: string; name: string; url: string; ref: string }

interface AdditionalReposListProps {
  repos: RepoWithId[]
  setRepos: React.Dispatch<React.SetStateAction<RepoWithId[]>>
}

export function AdditionalReposList({ repos, setRepos }: AdditionalReposListProps) {
  const updateRepo = (id: string, field: keyof RepoWithId, value: string) => {
    setRepos((prev) => prev.map((r) => (r.id === id ? { ...r, [field]: value } : r)))
  }

  return (
    <>
      {repos.map((repo) => (
        <div
          key={repo.id}
          className="bg-surface-elevated border border-border-default rounded-lg p-2.5 space-y-2"
        >
          <div className="flex items-center gap-2">
            <Input
              className="w-32"
              placeholder="Name"
              value={repo.name}
              onChange={(e) => updateRepo(repo.id, 'name', e.target.value)}
            />
            <Input
              className="flex-1"
              placeholder="URL"
              value={repo.url}
              onChange={(e) => updateRepo(repo.id, 'url', e.target.value)}
            />
            <Input
              className="w-24"
              placeholder="Ref"
              value={repo.ref}
              onChange={(e) => updateRepo(repo.id, 'ref', e.target.value)}
            />
            <button
              type="button"
              aria-label={`Remove repository ${repo.name || repo.id}`}
              className="p-1 rounded hover:bg-surface-hover text-text-tertiary hover:text-signal-red transition flex-shrink-0"
              onClick={() =>
                setRepos((prev) => prev.filter((r) => r.id !== repo.id))
              }
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      ))}
      <button
        type="button"
        className="text-xs text-text-link hover:text-signal-blue font-medium flex items-center gap-1"
        onClick={() =>
          setRepos((prev) => [
            ...prev,
            { id: crypto.randomUUID(), name: '', url: '', ref: '' },
          ])
        }
      >
        <Plus className="h-3.5 w-3.5" />
        Add Repository
      </button>
    </>
  )
}
