import { paginationResultValidator } from "convex/server";
import { v } from "convex/values";

export const userValidator = v.object({
  _id: v.id("users"),
  _creationTime: v.number(),
  tokenIdentifier: v.string(),
  name: v.string(),
  email: v.string(),
  pictureUrl: v.optional(v.string()),
  role: v.union(v.literal("user"), v.literal("admin")),
  createdAt: v.number(),
  updatedAt: v.optional(v.number()),
});

export const taskValidator = v.object({
  _id: v.id("tasks"),
  _creationTime: v.number(),
  userId: v.id("users"),
  title: v.string(),
  completed: v.boolean(),
  createdAt: v.number(),
});

export const taskPageValidator = paginationResultValidator(taskValidator);
