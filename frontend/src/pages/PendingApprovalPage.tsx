export function PendingApprovalPage() {
  return (
    <div className="flex min-h-screen items-center justify-center">
      <div className="rounded-xl border border-border-default bg-surface-card p-8 max-w-md text-center space-y-4">
        <h1 className="text-xl font-display font-bold text-text-primary">Account Pending Approval</h1>
        <p className="text-sm text-text-secondary">
          Your account has been created and is awaiting admin approval.
          You'll be able to access the application once an admin approves your registration.
        </p>
        <p className="text-xs text-text-tertiary">
          Please save your API key — you'll need it to log in once approved.
        </p>
      </div>
    </div>
  )
}
