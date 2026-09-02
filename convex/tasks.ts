import { paginationOptsValidator } from "convex/server";
import { v } from "convex/values";
import { Doc, Id } from "./_generated/dataModel";
import { MutationCtx, QueryCtx } from "./_generated/server";
import { internalMutation } from "./_generated/server";
import { UnauthorizedError } from "./lib/errors";
import { authedMutation, authedQuery } from "./lib/customFunctions";
import { taskPageValidator, taskValidator } from "./validators";

const THIRTY_DAYS_MS = 30 * 24 * 60 * 60 * 1000;
const CLEANUP_BATCH_SIZE = 100;

export const list = authedQuery({
  args: {
    paginationOpts: paginationOptsValidator,
  },
  returns: taskPageValidator,
  handler: async (ctx, args) => {
    return await ctx.db
      .query("tasks")
      .withIndex("by_user", (q) => q.eq("userId", ctx.user._id))
      .order("desc")
      .paginate(args.paginationOpts);
  },
});

export const listByStatus = authedQuery({
  args: {
    completed: v.boolean(),
    paginationOpts: paginationOptsValidator,
  },
  returns: taskPageValidator,
  handler: async (ctx, args) => {
    return await ctx.db
      .query("tasks")
      .withIndex("by_user_and_completed", (q) =>
        q.eq("userId", ctx.user._id).eq("completed", args.completed),
      )
      .order("desc")
      .paginate(args.paginationOpts);
  },
});

export const get = authedQuery({
  args: { taskId: v.id("tasks") },
  returns: v.union(taskValidator, v.null()),
  handler: async (ctx, args): Promise<Doc<"tasks"> | null> => {
    return await getOwnedTaskOrNull(ctx, ctx.user._id, args.taskId);
  },
});

export const create = authedMutation({
  args: { title: v.string() },
  returns: v.id("tasks"),
  handler: async (ctx, args): Promise<Id<"tasks">> => {
    return await createTaskForUser(ctx, ctx.user._id, args.title);
  },
});

export const update = authedMutation({
  args: {
    taskId: v.id("tasks"),
    title: v.optional(v.string()),
    completed: v.optional(v.boolean()),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    await updateOwnedTask(ctx, ctx.user._id, args);
    return null;
  },
});

export const remove = authedMutation({
  args: { taskId: v.id("tasks") },
  returns: v.null(),
  handler: async (ctx, args) => {
    await deleteOwnedTask(ctx, ctx.user._id, args.taskId);
    return null;
  },
});

export const cleanupOld = internalMutation({
  args: {},
  returns: v.object({ deleted: v.number() }),
  handler: async (ctx) => {
    const cutoff = Date.now() - THIRTY_DAYS_MS;
    const oldTasks = await ctx.db
      .query("tasks")
      .withIndex("by_completed_and_created", (q) =>
        q.eq("completed", true).lt("createdAt", cutoff),
      )
      .take(CLEANUP_BATCH_SIZE);

    for (const task of oldTasks) {
      await ctx.db.delete(task._id);
    }

    return { deleted: oldTasks.length };
  },
});

async function getOwnedTaskOrNull(
  ctx: QueryCtx | MutationCtx,
  userId: Id<"users">,
  taskId: Id<"tasks">,
): Promise<Doc<"tasks"> | null> {
  const task = await ctx.db.get(taskId);
  if (!task) {
    return null;
  }
  if (task.userId !== userId) {
    throw new UnauthorizedError("Unauthorized: You don't own this task");
  }
  return task;
}

async function requireOwnedTask(
  ctx: MutationCtx,
  userId: Id<"users">,
  taskId: Id<"tasks">,
): Promise<Doc<"tasks">> {
  const task = await getOwnedTaskOrNull(ctx, userId, taskId);
  if (!task) {
    throw new Error("Task not found");
  }
  return task;
}

async function createTaskForUser(
  ctx: MutationCtx,
  userId: Id<"users">,
  title: string,
): Promise<Id<"tasks">> {
  const trimmed = title.trim();
  if (trimmed.length === 0) {
    throw new Error("Title must not be empty");
  }
  if (trimmed.length > 200) {
    throw new Error("Title must be less than 200 characters");
  }

  return await ctx.db.insert("tasks", {
    userId,
    title: trimmed,
    completed: false,
    createdAt: Date.now(),
  });
}

async function updateOwnedTask(
  ctx: MutationCtx,
  userId: Id<"users">,
  args: {
    taskId: Id<"tasks">;
    title?: string;
    completed?: boolean;
  },
): Promise<void> {
  await requireOwnedTask(ctx, userId, args.taskId);

  const updates: {
    title?: string;
    completed?: boolean;
  } = {};

  if (args.title !== undefined) {
    const trimmed = args.title.trim();
    if (trimmed.length === 0) {
      throw new Error("Title must not be empty");
    }
    if (trimmed.length > 200) {
      throw new Error("Title must be less than 200 characters");
    }
    updates.title = trimmed;
  }

  if (args.completed !== undefined) {
    updates.completed = args.completed;
  }

  if (Object.keys(updates).length === 0) {
    return;
  }

  await ctx.db.patch(args.taskId, updates);
}

async function deleteOwnedTask(
  ctx: MutationCtx,
  userId: Id<"users">,
  taskId: Id<"tasks">,
): Promise<void> {
  await requireOwnedTask(ctx, userId, taskId);
  await ctx.db.delete(taskId);
}
