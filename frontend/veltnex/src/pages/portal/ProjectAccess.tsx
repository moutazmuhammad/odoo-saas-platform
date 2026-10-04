import * as React from "react";
import { Link, useParams } from "react-router-dom";
import { Copy, Pencil, FolderGit2, Plus, Search, ShieldCheck, Trash2, UserPlus, Users } from "lucide-react";
import { api, ApiError, type IamData } from "@/lib/api";
import { useToast } from "@/context/ToastContext";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { Dialog } from "@/components/ui/dialog";
import { AlertBanner } from "@/components/AlertBanner";
import { Spinner } from "@/components/Spinner";
import { ActionButton } from "@/components/ActionButton";

const SCOPES = ["production", "staging", "development"];
const toggle = <T,>(values: T[], value: T) => values.includes(value) ? values.filter(v => v !== value) : [...values, value];
type Grant = IamData["grants"][number];
type Person = { key: string; name: string; email: string; userId: number | null; groupId: number | null; grants: Grant[] };
const roleHelp: Record<string, string> = {
  viewer: "See project status, metrics, and build history.", logs: "Read application and deployment logs.",
  deploy: "Build, deploy, and upgrade changed Odoo modules.", operator: "Start, stop, and restart instances.",
  environment_creator: "Create staging and development servers in reserved slots.", environment_deleter: "Delete staging and development servers.",
  database_creator: "Create databases.", backup_operator: "Create database backups.", backup_downloader: "Download database backups.",
  database_restore: "Restore databases from backups.", database_deleter: "Delete databases.", database_access: "Reset database administrator passwords.",
  sql: "Use the SQL query tool.", terminal: "Open an instance terminal.", access_admin: "Manage teammates within your own access limits.",
  project_admin: "Manage all project tools and settings, including deletion. Billing stays with the owner.",
};
const categories = [
  { name: "View & operate", codes: ["viewer", "logs", "deploy", "operator"] },
  { name: "Environments", codes: ["environment_creator", "environment_deleter"] },
  { name: "Databases & backups", codes: ["database_creator", "backup_operator", "backup_downloader", "database_restore", "database_deleter", "database_access"] },
  { name: "Advanced access", codes: ["sql", "terminal", "access_admin", "project_admin"] },
];
const chip = "inline-flex items-center rounded-full border border-border bg-background/50 px-2.5 py-1 text-xs";

export default function ProjectAccess() {
  const { id } = useParams();
  const toast = useToast();
  const [data, setData] = React.useState<IamData | null>(null);
  const [error, setError] = React.useState("");
  const [tab, setTab] = React.useState("people");
  const [search, setSearch] = React.useState("");
  const [projectFilter, setProjectFilter] = React.useState(id || "all");
  const [personKey, setPersonKey] = React.useState<string | null>(null);
  const [wizard, setWizard] = React.useState(false);
  const [projects, setProjects] = React.useState<number[]>([]);
  const [roles, setRoles] = React.useState<string[]>(["viewer"]);
  const [scopes, setScopes] = React.useState<string[]>(["staging", "development"]);
  const [subject, setSubject] = React.useState("invite");
  const [email, setEmail] = React.useState("");
  const [inviteUrl, setInviteUrl] = React.useState("");
  const [roleSearch, setRoleSearch] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [remove, setRemove] = React.useState<{ ids: number[]; name: string } | null>(null);
  const [groupDelete, setGroupDelete] = React.useState(false);
  const [groupDraft, setGroupDraft] = React.useState<{ id?: number; name: string; user_ids: number[]; customer_id?: number } | null>(null);
  const load = React.useCallback(async () => {
    try { setData(await api.iam()); setError(""); }
    catch (e) { setError(e instanceof ApiError ? e.message : "Could not load project access."); }
  }, []);
  React.useEffect(() => { load(); }, [load]);
  React.useEffect(() => { setGroupDelete(false); }, [groupDraft?.id]);

  const people = React.useMemo(() => {
    const map = new Map<string, Person>();
    for (const g of data?.grants || []) {
      const customer = data?.projects.find(p => p.id === g.project_id)?.customer_id;
      const key = `${customer}:${g.group_id ? `group:${g.group_id}` : g.user_id ? `user:${g.user_id}` : `invite:${g.email}`}`;
      if (!map.has(key)) map.set(key, { key, name: g.name, email: g.email, userId: g.user_id, groupId: g.group_id, grants: [] });
      map.get(key)!.grants.push(g);
    }
    return [...map.values()];
  }, [data]);
  const visiblePeople = people.filter(p => (projectFilter === "all" || p.grants.some(g => g.project_id === Number(projectFilter))) && `${p.name} ${p.email}`.toLowerCase().includes(search.toLowerCase()));
  const person = people.find(p => p.key === personKey);
  const selected = data?.projects.filter(p => projects.includes(p.id)) || [];
  const customerId = selected[0]?.customer_id;
  const oneCustomer = selected.length > 0 && new Set(selected.map(p => p.customer_id)).size === 1;
  const groups = data?.groups.filter(g => g.customer_id === customerId) || [];
  const members = data?.members.filter(u => !u.customer_ids || u.customer_ids.includes(customerId!)) || [];
  const assignable = data?.roles.filter(r => selected.length && scopes.length && selected.every(p => scopes.every(e => p.assignable_roles[e]?.includes(r.code)))) || [];
  const validRoles = roles.length > 0 && roles.every(r => assignable.some(a => a.code === r));
  const validSubject = subject === "invite" ? /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim()) : subject.startsWith("user:") ? members.some(m => subject === `user:${m.id}`) : groups.some(g => subject === `group:${g.id}`);
  const openWizard = (target?: Person) => {
    setProjects(target ? [...new Set(target.grants.map(g => g.project_id))] : projectFilter !== "all" ? [Number(projectFilter)] : data?.projects.length === 1 ? [data.projects[0].id] : []);
    setSubject(target?.userId ? `user:${target.userId}` : target?.groupId ? `group:${target.groupId}` : "invite");
    setEmail(target?.email || ""); setRoles(["viewer"]); setScopes(["staging", "development"]);
    setRoleSearch(""); setPersonKey(null); setWizard(true);
  };
  const save = async () => {
    if (!oneCustomer || !validRoles || !validSubject || !scopes.length) return;
    setBusy(true);
    try {
      const params = { project_ids: projects, roles, environments: scopes };
      if (subject === "invite") {
        const result = await api.iamInvite({ ...params, email: email.trim() });
        setInviteUrl(result.invite_url);
        toast.success("Invitation sent", "Share the link if your teammate hasn’t received the email.");
      } else {
        const [kind, value] = subject.split(":");
        await api.iamGrant({ ...params, ...(kind === "group" ? { group_id: Number(value) } : { user_id: Number(value) }) });
        toast.success("Access added");
      }
      setWizard(false); await load();
    } catch (e) { toast.error("Could not save access", e instanceof ApiError ? e.message : "Please try again."); }
    finally { setBusy(false); }
  };
  const revoke = async () => {
    if (!remove) return;
    setBusy(true);
    try { await api.iamRevoke(remove.ids); setRemove(null); await load(); toast.success("Access removed"); }
    catch (e) { toast.error("Could not remove access", e instanceof ApiError ? e.message : "Please try again."); }
    finally { setBusy(false); }
  };
  const saveGroup = async () => {
    if (!groupDraft) return;
    setBusy(true);
    try { await api.iamGroup({ ...(groupDraft.id ? { group_id: groupDraft.id } : {}), name: groupDraft.name.trim(), user_ids: groupDraft.user_ids }); setGroupDraft(null); await load(); toast.success("Group saved"); }
    catch (e) { toast.error("Could not save group", e instanceof ApiError ? e.message : "Please try again."); }
    finally { setBusy(false); }
  };
  const deleteGroup = async () => {
    if (!groupDraft?.id) return;
    setBusy(true);
    try { await api.iamGroup({ group_id: groupDraft.id, delete: true }); setGroupDraft(null); setGroupDelete(false); await load(); toast.success("Group removed"); }
    catch (e) { toast.error("Could not remove group", e instanceof ApiError ? e.message : "Please try again."); }
    finally { setBusy(false); }
  };

  if (!data && !error) return <Spinner label="Loading project access…" />;
  return <div className="animate-fade-in space-y-6">
    <div className="flex flex-wrap items-center justify-between gap-4">
      <div><p className="mb-2 flex items-center gap-2 text-xs font-medium uppercase tracking-wider text-muted"><ShieldCheck className="size-4" />Team & permissions</p><h1 className="text-2xl font-bold tracking-tight">Project Access</h1><p className="mt-2 text-sm text-muted">Choose who can access your projects and what they can do.</p></div>
      {!!data?.projects.length && <Button onClick={() => openWizard()}><UserPlus />Grant access</Button>}
    </div>
    {error && <AlertBanner variant="danger" title="Couldn’t load access" description={error} />}
    {data && !data.projects.length && <Card className="flex flex-col items-center px-6 py-16 text-center">
      <span className="mb-5 rounded-2xl bg-primary/10 p-4"><FolderGit2 className="size-8 text-primary" /></span>
      <h2 className="text-lg font-semibold">{data.empty_reason === "no_projects" ? "No projects yet" : "Access management hasn’t been granted"}</h2>
      <p className="mt-2 max-w-md text-sm leading-relaxed text-muted">{data.empty_reason === "no_projects" ? "Create your first project, then invite teammates here. You’ll automatically have full control as its owner." : "Ask your project owner for the Project Access Administrator role to manage teammates and permissions."}</p>
      <Link className="mt-6 rounded-sm bg-primary px-5 py-2.5 text-sm font-medium text-primary-foreground" to={data.empty_reason === "no_projects" ? "/hosting" : "/my/instances"}>{data.empty_reason === "no_projects" ? "Create a project" : "Back to projects"}</Link>
    </Card>}
    {!!data?.projects.length && <>
      <div className="flex flex-wrap gap-6 border-b border-border" role="tablist" aria-label="Access views">
        {[{ key: "people", label: "Permissions", count: people.length }, { key: "groups", label: "Team groups", count: data.groups.length }].map(t => <button key={t.key} role="tab" aria-selected={tab === t.key} onClick={() => { setTab(t.key); setSearch(""); }} className={`border-b-2 pb-3 text-sm font-medium ${tab === t.key ? "border-primary text-primary" : "border-transparent text-muted"}`}>{t.label}<span className="ml-2 rounded-full bg-foreground/5 px-2 py-0.5 text-xs">{t.count}</span></button>)}
      </div>
      {tab === "people" ? <>
        <div className="flex flex-wrap items-center gap-3">
          <div className="relative flex-1"><Search className="absolute left-3 top-3 size-4 text-muted" /><Input aria-label="Search teammates" placeholder="Search by name or email…" className="pl-9" value={search} onChange={e => setSearch(e.target.value)} /></div>
          <select aria-label="Filter by project" className="h-10 rounded-sm border border-border bg-card px-3 text-sm" value={projectFilter} onChange={e => setProjectFilter(e.target.value)}><option value="all">All projects</option>{data.projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select>
        </div>
        <Card className="overflow-hidden">
          {visiblePeople.length ? <div className="overflow-x-auto"><table className="w-full min-w-[720px] text-left text-sm"><thead className="border-b border-border bg-foreground/3 text-xs text-muted"><tr>{["Type", "Principal", "Roles", "Projects", ""].map((h, i) => <th key={i} className="px-5 py-3 font-medium">{h}</th>)}</tr></thead><tbody className="divide-y divide-border">{visiblePeople.map(p => {
            const grants = p.grants.filter(g => projectFilter === "all" || g.project_id === Number(projectFilter));
            const pending = grants.every(g => g.pending);
            const projectNames = [...new Set(grants.map(g => data.projects.find(pr => pr.id === g.project_id)?.name))];
            return <tr key={p.key} onClick={() => setPersonKey(p.key)} className="cursor-pointer align-top hover:bg-foreground/3">
              <td className="px-5 py-4"><span className="text-muted" title={p.groupId ? "Team group" : "User"}>{p.groupId ? <Users className="size-4" /> : <UserPlus className="size-4" />}</span></td>
              <td className="px-5 py-4"><span className="font-medium">{p.email || p.name}</span>{p.email && p.name !== p.email && <p className="mt-1 text-xs text-muted">{p.name}</p>}{pending && <span className="mt-2 block text-xs text-warning">{grants.every(g => g.expired) ? "Invitation expired" : "Invitation pending"}</span>}</td>
              <td className="px-5 py-4"><div className="space-y-1">{[...new Set(grants.map(g => g.role))].map(role => <p key={role}>{data.roles.find(r => r.code === role)?.name}</p>)}</div></td>
              <td className="px-5 py-4"><div className="space-y-1">{projectNames.map(name => <p key={name}>{name}</p>)}</div></td>
              <td className="px-5 py-3"><Button size="icon" variant="ghost" aria-label={`Manage access for ${p.name}`} onClick={() => setPersonKey(p.key)}><Pencil /></Button></td>
            </tr>;
          })}</tbody></table></div> : <div className="flex flex-col items-center px-6 py-14 text-center"><Users className="mb-4 size-9 text-muted" /><h2 className="font-semibold">{people.length ? "No matching principals" : "No teammates yet"}</h2><p className="mt-2 max-w-sm text-sm text-muted">{people.length ? "Try another name or project." : "Use Grant access to invite your first teammate. Project owners already have full access."}</p></div>}
        </Card>
        <p className="flex items-center gap-2 text-xs text-muted"><ShieldCheck className="size-4" />Project owners keep full access. Billing is always managed by the owner.</p>
      </> : <>
        <div className="flex items-center justify-between gap-4"><p className="text-sm text-muted">Give the same project access to several teammates at once.</p>{(data.can_manage_groups ?? data.projects.some(p => p.is_owner)) && <Button variant="secondary" onClick={() => setGroupDraft({ name: "", user_ids: [], customer_id: data.current_customer_id })}><Plus />New group</Button>}</div>
        <div className="grid gap-4 sm:grid-cols-2">{data.groups.map(g => <Card key={g.id} className="p-5"><div className="flex items-center gap-3"><span className="rounded-lg bg-primary/10 p-3"><Users className="size-5 text-primary" /></span><div><h2 className="font-semibold">{g.name}</h2><p className="mt-1 text-xs text-muted">{g.user_ids.length} members</p></div></div><div className="mt-5 flex gap-2"><Button variant="secondary" disabled={g.editable === false || !data.projects.some(p => p.customer_id === g.customer_id && p.is_owner)} onClick={() => setGroupDraft({ ...g })}>Manage members</Button><Button variant="ghost" onClick={() => { openWizard(); setProjects(data.projects.filter(p => p.customer_id === g.customer_id).map(p => p.id)); setSubject(`group:${g.id}`); }}>Assign access</Button></div></Card>)}</div>
        {!data.groups.length && <Card className="p-10 text-center"><Users className="mx-auto mb-4 size-8 text-muted" /><h2 className="font-semibold">No team groups yet</h2><p className="mt-2 text-sm text-muted">Groups are optional. You can invite and manage teammates individually.</p></Card>}
      </>}
    </>}

    <Dialog open={wizard} onClose={() => !busy && setWizard(false)} title="Grant access" description="Select a principal and assign roles on your projects." variant="drawer" className="max-w-xl">
      <div className="space-y-6 py-4">
        {<>
          <fieldset><legend className="mb-3 text-sm font-medium">Which projects?</legend><div className="grid gap-2 sm:grid-cols-2">{data?.projects.map(p => <label key={p.id} className={`flex cursor-pointer items-center gap-3 rounded-lg border p-3 text-sm ${projects.includes(p.id) ? "border-primary/50 bg-primary/5" : "border-border"}`}><input type="checkbox" checked={projects.includes(p.id)} disabled={!!selected.length && customerId !== p.customer_id} onChange={() => { setProjects(toggle(projects, p.id)); if (customerId !== p.customer_id) setSubject("invite"); }} /><FolderGit2 className="size-4 text-muted" />{p.name}</label>)}</div><p className="mt-2 text-xs text-muted">Select one or more projects from the same customer.</p></fieldset>
          <div><Label htmlFor="access-subject">Who needs access?</Label><select id="access-subject" className="mt-2 h-10 w-full rounded-sm border border-border bg-card px-3 text-sm" value={subject} onChange={e => setSubject(e.target.value)}><option value="invite">Invite someone new by email</option>{members.map(m => <option key={m.id} value={`user:${m.id}`}>{m.name} · {m.email}</option>)}{groups.map(g => <option key={g.id} value={`group:${g.id}`}>Group: {g.name}</option>)}</select></div>
          {subject === "invite" && <div><Label htmlFor="access-email">Email address</Label><Input id="access-email" className="mt-2" type="email" placeholder="teammate@example.com" value={email} onChange={e => setEmail(e.target.value)} /><p className="mt-2 text-xs text-muted">Your teammate signs in with this email to accept the invitation.</p></div>}
        </>}
        {<>
          <fieldset><legend className="mb-3 text-sm font-medium">Where can they work?</legend><div className="flex flex-wrap gap-2">{SCOPES.map(e => <label key={e} className={`flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 text-sm capitalize ${scopes.includes(e) ? "border-primary/50 bg-primary/5" : "border-border"}`}><input type="checkbox" checked={scopes.includes(e)} onChange={() => setScopes(toggle(scopes, e))} />{e}</label>)}</div><p className="mt-2 text-xs text-muted">Production is excluded by default. Select it only if needed.</p></fieldset>
          <div><p className="mb-2 text-sm font-medium">What can they do?</p><Input aria-label="Find a role" placeholder="Find a role…" value={roleSearch} onChange={e => setRoleSearch(e.target.value)} /></div>
          {categories.map((category, n) => <details key={category.name} open={n === 0 || !!roleSearch || category.codes.some(c => roles.includes(c))} className="rounded-lg border border-border"><summary className="cursor-pointer px-4 py-3 text-sm font-medium">{category.name}</summary><div className="space-y-2 px-3 pb-3">{data?.roles.filter(r => category.codes.includes(r.code) && `${r.name} ${roleHelp[r.code]}`.toLowerCase().includes(roleSearch.toLowerCase())).map(r => {
            const allowed = assignable.some(a => a.code === r.code);
            return <label key={r.code} className={`flex items-start gap-3 rounded-md p-3 ${allowed ? "cursor-pointer hover:bg-foreground/3" : "opacity-50"}`}><input className="mt-1" type="checkbox" disabled={!allowed && !roles.includes(r.code)} checked={roles.includes(r.code)} onChange={() => setRoles(toggle(roles, r.code))} /><span><span className="block text-sm font-medium">{r.name}</span><span className="mt-1 block text-xs leading-relaxed text-muted">{roleHelp[r.code]}</span>{!allowed && <span className="mt-1 block text-xs text-warning">You can’t assign this role in the selected environments.</span>}</span></label>;
          })}</div></details>)}
          <p className="text-xs text-muted">Combine roles to give only the access needed. Billing access is never included.</p>
        </>}

      </div>
      <div className="sticky bottom-0 -mx-6 flex items-center justify-end gap-3 border-t border-border bg-card px-6 py-4"><Button variant="ghost" disabled={busy} onClick={() => setWizard(false)}>Cancel</Button><ActionButton loading={busy} disabled={!oneCustomer || !validSubject || !validRoles || !scopes.length} onClick={save}>Save</ActionButton></div>
    </Dialog>

    <Dialog open={!!person} onClose={() => setPersonKey(null)} title={person?.name || "Manage access"} description={person?.email || "Team group access"} variant="drawer" className="max-w-xl">
      {person && <><div className="space-y-4 py-4">{[...new Set(person.grants.map(g => g.project_id))].map(projectId => {
        const grants = person.grants.filter(g => g.project_id === projectId);
        return <div key={projectId} className="rounded-lg border border-border p-4"><h3 className="mb-4 text-sm font-semibold">{data?.projects.find(p => p.id === projectId)?.name}</h3><div className="divide-y divide-border">{[...new Set(grants.map(g => g.role))].map(role => {
          const bindings = grants.filter(g => g.role === role);
          return <div key={role} className="flex items-center justify-between gap-3 py-3"><div><p className="text-sm">{data?.roles.find(r => r.code === role)?.name}</p><p className="mt-1 text-xs capitalize text-muted">{bindings.some(g => g.environment === "all") ? "All environments" : [...new Set(bindings.map(g => g.environment))].join(", ")}</p></div><Button size="icon" variant="ghost" disabled={!bindings.every(g => g.editable)} aria-label={`Remove ${role} from ${person.name}`} onClick={() => setRemove({ ids: bindings.map(g => g.id), name: `${data?.roles.find(r => r.code === role)?.name} for ${person.name} on ${data?.projects.find(p => p.id === projectId)?.name}` })}><Trash2 /></Button></div>;
        })}</div></div>;
      })}</div><div className="flex flex-wrap justify-between gap-3 border-t border-border pt-4"><Button variant="danger" disabled={!person.grants.some(g => g.editable)} onClick={() => setRemove({ ids: person.grants.filter(g => g.editable).map(g => g.id), name: `the access you manage for ${person.name}` })}><Trash2 />Remove access</Button>{!person.grants.every(g => g.pending) && <Button onClick={() => openWizard(person)}><Plus />Add access</Button>}</div></>}
    </Dialog>
    <Dialog open={!!remove} onClose={() => !busy && setRemove(null)} title="Remove access?" description={`Remove ${remove?.name || "this role"}? Other access stays unchanged.`}><div className="mt-5 flex justify-end gap-3"><Button variant="secondary" disabled={busy} onClick={() => setRemove(null)}>Keep access</Button><ActionButton variant="danger" loading={busy} onClick={revoke}>Remove access</ActionButton></div></Dialog>
    <Dialog open={!!inviteUrl} onClose={() => setInviteUrl("")} title="Invitation ready" description="Your teammate can accept using this link."><div className="mt-4 space-y-4"><Input aria-label="Invitation link" readOnly value={inviteUrl} /><Button className="w-full" onClick={async () => { try { await navigator.clipboard.writeText(inviteUrl); toast.success("Link copied"); } catch { toast.error("Couldn’t copy", "Select and copy the link above."); } }}><Copy />Copy invitation link</Button><p className="text-xs text-muted">An invitation email is queued. They must sign in with the invited email address.</p></div></Dialog>
    <Dialog open={!!groupDraft} onClose={() => !busy && setGroupDraft(null)} title={groupDraft?.id ? "Manage team group" : "Create a team group"} description="Members share the roles assigned to this group."><div className="mt-4 space-y-4"><div><Label htmlFor="group-name">Group name</Label><Input id="group-name" className="mt-2" placeholder="For example, Developers" value={groupDraft?.name || ""} onChange={e => setGroupDraft(d => d && { ...d, name: e.target.value })} /></div><fieldset className="max-h-52 space-y-2 overflow-y-auto"><legend className="mb-2 text-sm font-medium">Members</legend>{data?.members.filter(m => !groupDraft?.customer_id || !m.customer_ids || m.customer_ids.includes(groupDraft.customer_id)).map(m => <label key={m.id} className="flex items-center gap-3 rounded-md border border-border p-3 text-sm"><input type="checkbox" checked={groupDraft?.user_ids.includes(m.id) || false} onChange={() => setGroupDraft(d => d && { ...d, user_ids: toggle(d.user_ids, m.id) })} /><span>{m.name}<span className="mt-1 block text-xs text-muted">{m.email}</span></span></label>)}{!data?.members.length && <p className="text-sm text-muted">Invite teammates first. They’ll appear here after accepting.</p>}</fieldset><div className="flex justify-between gap-3 border-t border-border pt-4">{groupDraft?.id ? <Button variant="danger" disabled={busy} onClick={() => setGroupDelete(true)}><Trash2 />Delete group</Button> : <Button variant="ghost" onClick={() => setGroupDraft(null)}>Cancel</Button>}<ActionButton loading={busy} disabled={!groupDraft?.name.trim()} onClick={saveGroup}>Save group</ActionButton></div></div></Dialog>
    <Dialog open={groupDelete} onClose={() => !busy && setGroupDelete(false)} title="Delete team group?" description="Group members will lose the access provided by this group. Their individual roles stay unchanged."><div className="mt-5 flex justify-end gap-3"><Button variant="secondary" disabled={busy} onClick={() => setGroupDelete(false)}>Keep group</Button><ActionButton variant="danger" loading={busy} onClick={deleteGroup}>Delete group</ActionButton></div></Dialog>
  </div>;
}
