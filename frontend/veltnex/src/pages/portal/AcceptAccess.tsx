import * as React from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import { api, ApiError } from "@/lib/api";
import { useInstances } from "@/context/InstancesContext";
import { Card } from "@/components/ui/card";
import { ActionButton } from "@/components/ActionButton";
import { AlertBanner } from "@/components/AlertBanner";

export default function AcceptAccess() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { reload } = useInstances();
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const token = params.get("token") || "";
  const accept = async () => {
    setBusy(true); setError("");
    try { const result = await api.iamAccept(token); await reload(); navigate(`/my/instances/${result.project_ids[0]}`, { replace: true }); }
    catch (e) { setError(e instanceof ApiError ? e.message : "Could not accept invitation."); setBusy(false); }
  };
  return <Card className="mx-auto max-w-lg space-y-5 p-8"><h1 className="text-2xl font-bold">Accept project invitation</h1><p className="text-muted">You must be signed in with the email address this invitation was sent to. Your roles apply only to the assigned projects and environments.</p>{error && <AlertBanner variant="danger" title="Invitation" description={error} />}<ActionButton loading={busy} disabled={!token} onClick={accept}>Accept invitation</ActionButton></Card>;
}
