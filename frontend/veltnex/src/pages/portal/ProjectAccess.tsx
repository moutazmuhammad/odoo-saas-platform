import * as React from "react";
import { Link, useParams } from "react-router-dom";
import { ShieldCheck, UserPlus, Users, Trash2, Copy } from "lucide-react";
import { api, ApiError, type IamData } from "@/lib/api";
import { useToast } from "@/context/ToastContext";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { AlertBanner } from "@/components/AlertBanner";
import { Spinner } from "@/components/Spinner";
import { ActionButton } from "@/components/ActionButton";

const SCOPES = ["production", "staging", "development"];
const toggle = <T,>(values: T[], value: T) => values.includes(value) ? values.filter(v => v !== value) : [...values, value];

export default function ProjectAccess() {
  const { id } = useParams();
  const toast = useToast();
  const [data, setData] = React.useState<IamData | null>(null);
  const [error, setError] = React.useState("");
  const [projects, setProjects] = React.useState<number[]>(id ? [Number(id)] : []);
  const [roles, setRoles] = React.useState<string[]>(["viewer"]);
  const [scopes, setScopes] = React.useState<string[]>(["staging", "development"]);
  const [subject, setSubject] = React.useState("invite");
  const [email, setEmail] = React.useState("");
  const [inviteGroup, setInviteGroup] = React.useState("");
  const [inviteUrl, setInviteUrl] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [groupName, setGroupName] = React.useState("");
  const load = React.useCallback(async () => {
    try { setData(await api.iam()); setError(""); }
    catch (e) { setError(e instanceof ApiError ? e.message : "Could not load project access."); }
  }, []);
  React.useEffect(() => { load(); }, [load]);
  const selected = data?.projects.filter(p => projects.includes(p.id)) || [];
  const oneCustomer = selected.length > 0 && new Set(selected.map(p => p.customer_id)).size === 1;
  const assignable = data?.roles.filter(r => selected.length && scopes.length && selected.every(p => scopes.every(e => p.assignable_roles[e]?.includes(r.code)))) || [];
  const canSave = oneCustomer && roles.length > 0 && scopes.length > 0 && roles.every(r => assignable.some(a => a.code === r));
  const ownerSelection = selected.length > 0 && selected.every(p => p.is_owner);
  const groups = data?.groups.filter(g => g.customer_id === selected[0]?.customer_id) || [];
  const members = data?.members.filter(u => !u.customer_ids || u.customer_ids.includes(selected[0]?.customer_id)) || [];
  const ownedCustomers = data?.projects.filter(p => p.is_owner).map(p => p.customer_id) || [];
  const save = async () => {
    setBusy(true); setInviteUrl("");
    try {
      const params = { project_ids: projects, roles, environments: scopes };
      if (subject === "invite") {
        const result = await api.iamInvite({ ...params, email: email.trim(), ...(inviteGroup ? { group_id: Number(inviteGroup) } : {}) });
        setInviteUrl(result.invite_url); setEmail("");
        toast.success("Invitation created", "An email is queued. You can also copy the invitation link.");
      } else {
        const [kind, value] = subject.split(":");
        await api.iamGrant({ ...params, ...(kind === "group" ? { group_id: Number(value) } : { user_id: Number(value) }) });
        toast.success("Project access saved");
      }
      await load();
    } catch (e) { toast.error("Access could not be saved", e instanceof ApiError ? e.message : "Please try again."); }
    finally { setBusy(false); }
  };
  const revoke = async (grantId: number) => {
    setBusy(true);
    try { await api.iamRevoke([grantId]); await load(); toast.success("Role removed"); }
    catch (e) { toast.error("Could not remove role", e instanceof ApiError ? e.message : "Please try again."); }
    finally { setBusy(false); }
  };
  const createGroup = async () => {
    setBusy(true);
    try { await api.iamGroup({ name: groupName }); setGroupName(""); await load(); toast.success("Team group created"); }
    catch (e) { toast.error("Could not create group", e instanceof ApiError ? e.message : "Please try again."); }
    finally { setBusy(false); }
  };
  const updateGroup = async (groupId: number, userIds: number[]) => {
    setBusy(true);
    try { await api.iamGroup({ group_id: groupId, user_ids: userIds }); await load(); }
    catch (e) { toast.error("Could not update group", e instanceof ApiError ? e.message : "Please try again."); }
    finally { setBusy(false); }
  };
  const deleteGroup = async (groupId: number) => {
    setBusy(true);
    try { await api.iamGroup({ group_id: groupId, delete: true }); await load(); toast.success("Team group removed"); }
    catch (e) { toast.error("Could not remove group", e instanceof ApiError ? e.message : "Please try again."); }
    finally { setBusy(false); }
  };
  if (!data && !error) return <Spinner label="Loading project access…" />;
  return <div className="animate-fade-in space-y-6">
    <div><h1 className="flex items-center gap-2 text-2xl font-bold"><ShieldCheck className="size-6 text-primary" />Project Access</h1>
      <p className="mt-2 text-sm text-muted">Give teammates fixed roles on selected projects and environments. Billing stays with the customer owner.</p></div>
    {error && <AlertBanner variant="danger" title="Project access" description={error} />}
    {data && data.projects.length === 0 && <Card className="space-y-3 p-6">
      {data.empty_reason === "no_projects" ? <>
        <h2 className="font-semibold">No projects yet</h2>
        <p className="text-sm text-muted">Create your first project to invite teammates and assign their roles. As the project owner, you can manage access automatically.</p>
        <Link className="inline-block text-primary" to="/hosting">Create a project</Link>
      </> : <>
        <h2 className="font-semibold">Access management hasn’t been granted</h2>
        <p className="text-sm text-muted">Ask the project owner to give you the Project Access Administrator role to manage teammates’ access.</p>
        <Link className="inline-block text-primary" to="/my/instances">Back to projects</Link>
      </>}
    </Card>}
    {!!data?.projects.length && <>
    <Card className="space-y-5 p-6">
      <h2 className="flex items-center gap-2 text-lg font-semibold"><UserPlus className="size-5" />Grant project access</h2>
      <div><Label>Teammate or team group</Label><select className="mt-2 h-10 w-full rounded-lg border border-border bg-card px-3" value={subject} onChange={e => setSubject(e.target.value)}>
        <option value="invite">Invite by email</option>
        {members.map(u => <option key={u.id} value={`user:${u.id}`}>{u.name} · {u.email}</option>)}
        {groups.map(g => <option key={g.id} value={`group:${g.id}`}>Team group: {g.name}</option>)}
      </select>{subject === "invite" && <Input className="mt-3" type="email" placeholder="teammate@example.com" value={email} onChange={e => setEmail(e.target.value)} />}</div>
      <fieldset><legend className="text-sm font-medium">Projects</legend><div className="mt-2 flex flex-wrap gap-3">{data.projects.map(p => <label key={p.id} className="flex items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm"><input type="checkbox" checked={projects.includes(p.id)} onChange={() => { setProjects(toggle(projects, p.id)); setInviteGroup(""); }} />{p.name}</label>)}</div></fieldset>
      {selected.length > 1 && !oneCustomer && <p className="text-sm text-danger">Select projects belonging to the same customer.</p>}
      <fieldset><legend className="text-sm font-medium">Environments</legend><div className="mt-2 flex flex-wrap gap-4">{SCOPES.map(e => <label key={e} className="flex items-center gap-2 text-sm capitalize"><input type="checkbox" checked={scopes.includes(e)} onChange={() => setScopes(toggle(scopes, e))} />{e}</label>)}<button type="button" className="text-sm text-primary" onClick={() => setScopes(SCOPES)}>Select all</button></div></fieldset>
      <fieldset><legend className="text-sm font-medium">Fixed roles</legend><div className="mt-2 grid gap-2 sm:grid-cols-2">{data.roles.map(r => {
        const permitted = assignable.some(a => a.code === r.code);
        return <label key={r.code} className={`flex items-start gap-2 rounded-lg border border-border p-3 text-sm ${permitted ? "" : "opacity-50"}`} title={r.permissions.join(", ")}><input className="mt-1" type="checkbox" disabled={!permitted} checked={roles.includes(r.code)} onChange={() => setRoles(toggle(roles, r.code))} /><span>{r.name}</span></label>;
      })}</div><p className="mt-2 text-xs text-muted">Access administrators can grant only roles they hold in the selected environment scopes.</p></fieldset>
      {subject === "invite" && ownerSelection && groups.length > 0 && <div><Label>Add to a team group after acceptance (optional)</Label><select className="mt-2 h-10 w-full rounded-lg border border-border bg-card px-3" value={inviteGroup} onChange={e => setInviteGroup(e.target.value)}><option value="">No group</option>{groups.map(g => <option key={g.id} value={g.id}>{g.name}</option>)}</select></div>}
      <ActionButton loading={busy} disabled={!canSave || (subject === "invite" && !email.includes("@"))} onClick={save}>{subject === "invite" ? "Send invitation" : "Grant roles"}</ActionButton>
      {inviteUrl && <div className="rounded-lg border border-success/30 p-4"><p className="text-sm">Invitation created. The teammate must sign in with the invited email.</p><div className="mt-2 flex gap-2"><Input readOnly value={inviteUrl} /><Button variant="secondary" aria-label="Copy invitation link" onClick={async () => { await navigator.clipboard.writeText(inviteUrl); toast.success("Invitation link copied"); }}><Copy className="size-4" /></Button></div></div>}
    </Card>
    <Card className="overflow-hidden"><h2 className="p-5 text-lg font-semibold">Current access</h2><div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead className="border-y border-border text-muted"><tr>{["Teammate / group", "Project", "Role", "Environment", ""].map((h, i) => <th key={i} className="px-5 py-3">{h}</th>)}</tr></thead><tbody>{data.grants.map(g => <tr key={g.id} className="border-b border-border"><td className="px-5 py-3"><p>{g.name}</p><p className="text-xs text-muted">{g.pending ? g.expired ? "Invitation expired" : "Invitation pending" : g.email}</p></td><td className="px-5 py-3">{data.projects.find(p => p.id === g.project_id)?.name}</td><td className="px-5 py-3">{data.roles.find(r => r.code === g.role)?.name}</td><td className="px-5 py-3 capitalize">{g.environment}</td><td className="px-5 py-3"><Button variant="ghost" disabled={busy || !g.editable} aria-label={`Remove ${g.role} from ${g.name}`} onClick={() => revoke(g.id)}><Trash2 className="size-4" /></Button></td></tr>)}</tbody></table></div>{data.grants.length === 0 && <p className="p-5 text-sm text-muted">No teammate access has been granted yet. The project owner keeps full access.</p>}</Card>
    {data.projects.some(p => p.is_owner) && <Card className="space-y-4 p-6"><h2 className="flex items-center gap-2 text-lg font-semibold"><Users className="size-5" />Team groups</h2><div className="flex gap-2"><Input placeholder="Group name" value={groupName} onChange={e => setGroupName(e.target.value)} /><Button disabled={busy || !groupName.trim()} onClick={createGroup}>Create group</Button></div>{data.groups.filter(g => ownedCustomers.includes(g.customer_id)).map(g => <div key={g.id} className="rounded-lg border border-border p-4"><div className="flex items-center justify-between"><strong>{g.name}</strong><Button variant="ghost" disabled={busy} aria-label={`Delete ${g.name}`} onClick={() => deleteGroup(g.id)}><Trash2 className="size-4" /></Button></div><div className="mt-2 flex flex-wrap gap-3">{data.members.filter(u => !u.customer_ids || u.customer_ids.includes(g.customer_id)).map(u => <label key={u.id} className="flex items-center gap-2 text-sm"><input type="checkbox" disabled={busy} checked={g.user_ids.includes(u.id)} onChange={() => updateGroup(g.id, toggle(g.user_ids, u.id))} />{u.name}</label>)}{data.members.length === 0 && <p className="text-sm text-muted">Invite teammates first; they become available after accepting.</p>}</div></div>)}</Card>}
    </>}
  </div>;
}
