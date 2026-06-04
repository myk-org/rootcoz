import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'

import { AuthProvider } from '@/lib/auth'
import { Layout } from '@/components/layout/Layout'
import { ProtectedRoute } from '@/components/shared/ProtectedRoute'

import { RegisterPage } from '@/pages/RegisterPage'
import { DashboardPage } from '@/pages/DashboardPage'
import { StatusPage } from '@/pages/StatusPage'
import { SettingsPage } from '@/pages/SettingsPage'
import { HistoryPage } from '@/pages/HistoryPage'
import { ReportPage } from '@/pages/ReportPage'
import { ChatPage } from '@/pages/ChatPage'
import { TestHistoryPage } from '@/pages/TestHistoryPage'
import { UsersPage } from '@/pages/UsersPage'
import { TokenUsagePage } from '@/pages/TokenUsagePage'
import { MentionsPage } from '@/pages/MentionsPage'
import { NewAnalysisPage } from '@/pages/NewAnalysisPage'
import { PendingApprovalPage } from '@/pages/PendingApprovalPage'
import { ServerSettingsPage } from '@/pages/ServerSettingsPage'
import { AdminChatPage } from '@/pages/AdminChatPage'
import { ReportsPage } from '@/pages/ReportsPage'

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter basename="/">
        <Routes>
          <Route path="/login" element={<RegisterPage />} />
          <Route path="/pending" element={<PendingApprovalPage />} />
          <Route element={<Layout />}>
            <Route index element={<ProtectedRoute><DashboardPage /></ProtectedRoute>} />
            <Route path="/dashboard" element={<Navigate to="/" replace />} />
            <Route path="/new-analysis" element={<ProtectedRoute operatorOnly><NewAnalysisPage /></ProtectedRoute>} />
            <Route path="/history" element={<ProtectedRoute><HistoryPage /></ProtectedRoute>} />
            <Route path="/reports" element={<ProtectedRoute adminOnly><ReportsPage /></ProtectedRoute>} />
            <Route path="/history/test/:testName" element={<ProtectedRoute><TestHistoryPage /></ProtectedRoute>} />
            <Route path="/mentions" element={<ProtectedRoute><MentionsPage /></ProtectedRoute>} />
            <Route path="/results/:jobId" element={<ProtectedRoute><ReportPage /></ProtectedRoute>} />
            <Route path="/chat/:jobId" element={<ProtectedRoute><ChatPage /></ProtectedRoute>} />
            <Route path="/status/:jobId" element={<ProtectedRoute><StatusPage /></ProtectedRoute>} />
            <Route path="/settings" element={<ProtectedRoute><SettingsPage /></ProtectedRoute>} />
            <Route path="/admin/users" element={<ProtectedRoute adminOnly><UsersPage /></ProtectedRoute>} />
            <Route path="/admin/token-usage" element={<ProtectedRoute adminOnly><TokenUsagePage /></ProtectedRoute>} />
            <Route path="/admin/settings" element={<ProtectedRoute adminOnly><ServerSettingsPage /></ProtectedRoute>} />
            <Route path="/admin/chat" element={<ProtectedRoute adminOnly><AdminChatPage /></ProtectedRoute>} />
          </Route>
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  )
}
