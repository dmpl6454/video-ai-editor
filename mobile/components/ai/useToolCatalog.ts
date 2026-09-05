/**
 * Fetching what this particular Mac can do.
 *
 * TWO INDEPENDENT REQUESTS, IN PARALLEL, WITH INDEPENDENT FAILURES. `/api/tools`
 * says which tools exist; `/api/features` says which optional dependencies are
 * installed. Chaining them would make the slower one — the feature probe, which
 * costs the Mac a couple of seconds of `ai.*` imports on a cold start — delay
 * the list of cards for no reason. Worse, folding their errors together would
 * mean a failed feature probe emptied the screen, when the honest outcome is a
 * full list of cards with "checking" badges: `gateFor` treats an absent report
 * as UNKNOWN, not as unavailable, so every tool stays runnable and a missing
 * dependency surfaces as a 422 instead of a control that was greyed out on a
 * guess.
 *
 * FETCHED WHEN THE SCREEN OPENS, NOT AT LAUNCH. Same reasoning as the desktop:
 * the probe is expensive and lands at exactly the moment the first thumbnails
 * and uploads do. A user who never opens this screen should never pay for it.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import type { ApiClient } from "../../lib/api";
import { errorMessage } from "../../lib/errors";
import type { FeatureReport, ToolSchema } from "../../lib/types";

export interface ToolCatalogState {
  /** null until `/api/tools` answers. */
  tools: ToolSchema[] | null;
  /** null while unknown — see the header; this is not the same as "none". */
  features: FeatureReport | null;
  loading: boolean;
  toolsError: string | null;
  featuresError: string | null;
  reload: () => void;
}

export function useToolCatalog(client: ApiClient | null): ToolCatalogState {
  const [tools, setTools] = useState<ToolSchema[] | null>(null);
  const [features, setFeatures] = useState<FeatureReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [toolsError, setToolsError] = useState<string | null>(null);
  const [featuresError, setFeaturesError] = useState<string | null>(null);
  // Guards against a slow first response landing after the user has pulled to
  // refresh, which would otherwise overwrite the newer answer with the older.
  const generation = useRef(0);

  const load = useCallback(async () => {
    if (client === null) return;
    const mine = ++generation.current;
    setLoading(true);
    const [toolsResult, featuresResult] = await Promise.allSettled([
      client.getTools(),
      client.getFeatures(),
    ]);
    if (mine !== generation.current) return;

    if (toolsResult.status === "fulfilled") {
      setTools(toolsResult.value.tools);
      setToolsError(null);
    } else {
      setToolsError(errorMessage(toolsResult.reason));
    }

    if (featuresResult.status === "fulfilled") {
      setFeatures(featuresResult.value);
      setFeaturesError(null);
    } else {
      setFeaturesError(errorMessage(featuresResult.reason));
    }

    setLoading(false);
  }, [client]);

  useEffect(() => {
    void load();
  }, [load]);

  const reload = useCallback(() => {
    void load();
  }, [load]);

  return { tools, features, loading, toolsError, featuresError, reload };
}
