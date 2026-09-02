import { v } from "convex/values";
import { Doc, Id } from "./_generated/dataModel";
import { MutationCtx, query } from "./_generated/server";
import { getCurrentUserOrNull } from "./lib/auth";
import { mutation } from "./_generated/server";
import { userValidator } from "./validators";

export const me = query({
  args: {},
  returns: v.union(userValidator, v.null()),
  handler: async (ctx): Promise<Doc<"users"> | null> => {
    return await getCurrentUserOrNull(ctx);
  },
});

export const store = mutation({
  args: {},
  returns: v.id("users"),
  handler: async (ctx): Promise<Id<"users">> => {
    return await storeCurrentUser(ctx);
  },
});

async function storeCurrentUser(ctx: MutationCtx): Promise<Id<"users">> {
  const identity = await ctx.auth.getUserIdentity();
  if (!identity) {
    throw new Error("Not authenticated");
  }

  const existing = await ctx.db
    .query("users")
    .withIndex("by_token", (q) =>
      q.eq("tokenIdentifier", identity.tokenIdentifier),
    )
    .unique();

  if (existing) {
    await ctx.db.patch(existing._id, {
      name: identity.name ?? existing.name,
      email: identity.email ?? existing.email,
      pictureUrl: identity.pictureUrl ?? existing.pictureUrl,
      updatedAt: Date.now(),
    });
    return existing._id;
  }

  return await ctx.db.insert("users", {
    tokenIdentifier: identity.tokenIdentifier,
    name: identity.name ?? "Anonymous",
    email: identity.email ?? "",
    pictureUrl: identity.pictureUrl,
    role: "user",
    createdAt: Date.now(),
  });
}
