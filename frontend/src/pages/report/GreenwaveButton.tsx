import { useState } from 'react'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Upload } from 'lucide-react'
import { ExporterPushButton } from '@/components/shared/ExporterPushButton'
import { exporterEnabled, useReportState } from '@/pages/report/reportState'
import { GreenwavePushForm } from './GreenwavePushForm'
import type { FailureAnalysis } from '@/types'

interface GreenwaveButtonProps {
  jobId: string
  jobName: string
  buildNumber?: number | string
  childJobName?: string
  childBuildNumber?: number
  failures: FailureAnalysis[]
}

export function GreenwaveButton({ jobId, jobName, buildNumber, childJobName, childBuildNumber, failures }: GreenwaveButtonProps) {
  const { exporters } = useReportState()
  const [confirmOpen, setConfirmOpen] = useState(false)

  if (!exporterEnabled(exporters, 'greenwave')) return null

  return (
    <>
      <ExporterPushButton
        label="Push to Greenwave"
        pushingLabel="Pushing..."
        pushing={false}
        disabled={failures.length === 0}
        disabledHint="No failures to push"
        onClick={() => setConfirmOpen(true)}
      />

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent className="sm:max-w-[520px] bg-surface-card border-border-default">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Upload className="h-5 w-5 text-text-secondary" />
              Push to Greenwave
            </DialogTitle>
          </DialogHeader>
          <GreenwavePushForm
            jobId={jobId}
            jobName={jobName}
            buildNumber={buildNumber}
            childJobName={childJobName}
            childBuildNumber={childBuildNumber}
            failures={failures}
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
