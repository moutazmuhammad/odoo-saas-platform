import { useParams } from "react-router-dom";
import SqlConsole from "./SqlConsole";

/** Standalone SQL console page. Identity/header is provided by InstanceLayout,
 *  or by the Environments workspace when embedded (`embedId`). */
export default function SqlPage({ embedId }: { embedId?: number } = {}) {
  const { id = "" } = useParams();
  const instanceId = embedId != null ? embedId : Number(id);
  return (
    <div>
      <SqlConsole instanceId={instanceId} />
    </div>
  );
}
