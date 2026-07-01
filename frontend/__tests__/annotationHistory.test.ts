import { type AnnotationCommand, createAnnotationHistory } from "@/lib/annotationHistory";
import { describe, expect, it } from "vitest";

const create = (id: string): AnnotationCommand => ({
  type: "create",
  csId: id,
  annotation: { id },
});
const remove = (id: string): AnnotationCommand => ({
  type: "remove",
  csId: id,
  annotation: { id },
});

describe("annotationHistory", () => {
  it("push clears redo and tracks depths", () => {
    const h = createAnnotationHistory();
    expect(h.canUndo()).toBe(false);
    expect(h.canRedo()).toBe(false);
    h.push(create("a"));
    h.push(create("b"));
    expect(h.undoDepth()).toBe(2);
    expect(h.canUndo()).toBe(true);
  });

  it("undo moves the newest command to redo and returns it (to invert)", () => {
    const h = createAnnotationHistory();
    h.push(create("a"));
    h.push(remove("b"));
    const undone = h.undo();
    expect(undone).toEqual(remove("b"));
    expect(h.undoDepth()).toBe(1);
    expect(h.redoDepth()).toBe(1);
    expect(h.canRedo()).toBe(true);
  });

  it("redo moves it back and returns it (to apply forward)", () => {
    const h = createAnnotationHistory();
    h.push(create("a"));
    h.undo();
    const redone = h.redo();
    expect(redone).toEqual(create("a"));
    expect(h.undoDepth()).toBe(1);
    expect(h.redoDepth()).toBe(0);
  });

  it("a new push after undo clears the redo stack (branch discarded)", () => {
    const h = createAnnotationHistory();
    h.push(create("a"));
    h.push(create("b"));
    h.undo(); // b -> redo
    expect(h.canRedo()).toBe(true);
    h.push(create("c"));
    expect(h.canRedo()).toBe(false);
    expect(h.undoDepth()).toBe(2); // a, c
  });

  it("undo/redo on an empty stack returns null", () => {
    const h = createAnnotationHistory();
    expect(h.undo()).toBeNull();
    expect(h.redo()).toBeNull();
  });

  it("is bounded to the limit (oldest dropped)", () => {
    const h = createAnnotationHistory(3);
    h.push(create("a"));
    h.push(create("b"));
    h.push(create("c"));
    h.push(create("d")); // drops "a"
    expect(h.undoDepth()).toBe(3);
    // Undo all three should yield d, c, b — never the dropped "a".
    expect(h.undo()).toEqual(create("d"));
    expect(h.undo()).toEqual(create("c"));
    expect(h.undo()).toEqual(create("b"));
    expect(h.undo()).toBeNull();
  });

  it("clear empties both stacks", () => {
    const h = createAnnotationHistory();
    h.push(create("a"));
    h.undo();
    h.clear();
    expect(h.canUndo()).toBe(false);
    expect(h.canRedo()).toBe(false);
  });
});
