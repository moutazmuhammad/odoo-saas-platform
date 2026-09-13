import { useParams } from "react-router-dom";
import ShellConsole from "./ShellConsole";

/** Standalone Shell page. Identity/header is provided by InstanceLayout, or by
 *  the Environments workspace when embedded (`embedId`). */
export default function ShellPage({ embedId }: { embedId?: number } = {}) {
  const { id = "" } = useParams();
  const instanceId = embedId != null ? embedId : Number(id);
  return (
    <div>
      <ShellConsole instanceId={instanceId} />
    </div>
  );
}
