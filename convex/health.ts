import { v } from "convex/values";
import { query } from "./_generated/server";

export const status = query({
  args: {},
  returns: v.object({
    ok: v.literal(true),
    product: v.string(),
    tables: v.array(v.string()),
  }),
  handler: async () => {
    return {
      ok: true as const,
      product: "CRABDECK X",
      tables: ["users", "tasks"],
    };
  },
});
