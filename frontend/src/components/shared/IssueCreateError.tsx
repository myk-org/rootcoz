/** The create-failure notice both issue dialogs show above their preview. */
export function IssueCreateError({ message }: { message: string }) {
  return (
    <div role="alert" className="space-y-1">
      <p className="text-sm text-signal-red">{message}</p>
      {message.toLowerCase().includes('token') && (
        <p className="text-xs text-text-tertiary">You can update your tokens in <a href="/settings" target="_blank" rel="noopener noreferrer" className="text-text-link hover:underline">Profile Settings</a>.</p>
      )}
    </div>
  )
}
