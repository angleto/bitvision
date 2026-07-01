// Bounded undo/redo command stack for viewer annotations (task cde63ced).
//
// Kept as a PURE data structure with NO Cornerstone dependency so it is fully
// unit-testable: it only tracks the sequence of annotation mutations and hands
// the caller back the command to invert (create ↔ remove, label old ↔ new).
// The viewer page maps each returned command to the concrete Cornerstone /
// markersApi effect (removeAnnotation / restoreAnnotation / updateAnnotationLabel).
//
// Semantics (standard editor undo model):
//   push(cmd)  records a user mutation and clears the redo stack.
//   undo()     moves the newest command to the redo stack and returns it — the
//              caller applies its INVERSE.
//   redo()     moves it back to the undo stack and returns it — the caller
//              applies it FORWARD.
// Bounded to ``limit`` entries so a long session can't grow unboundedly; the
// oldest command is dropped when the cap is exceeded.

export type AnnotationCommand =
  | { type: "create"; csId: string; annotation: unknown }
  | { type: "remove"; csId: string; annotation: unknown }
  | { type: "label"; csId: string; prevLabel: string; nextLabel: string };

export interface AnnotationHistory {
  /** Record a user mutation; clears the redo stack. */
  push(cmd: AnnotationCommand): void;
  /** Pop the newest command onto the redo stack and return it (invert it). */
  undo(): AnnotationCommand | null;
  /** Move the newest redo command back and return it (apply forward). */
  redo(): AnnotationCommand | null;
  canUndo(): boolean;
  canRedo(): boolean;
  undoDepth(): number;
  redoDepth(): number;
  clear(): void;
}

export function createAnnotationHistory(limit = 50): AnnotationHistory {
  let undoStack: AnnotationCommand[] = [];
  let redoStack: AnnotationCommand[] = [];

  return {
    push(cmd) {
      undoStack.push(cmd);
      if (undoStack.length > limit) undoStack = undoStack.slice(-limit);
      redoStack = [];
    },
    undo() {
      const cmd = undoStack.pop();
      if (!cmd) return null;
      redoStack.push(cmd);
      return cmd;
    },
    redo() {
      const cmd = redoStack.pop();
      if (!cmd) return null;
      undoStack.push(cmd);
      return cmd;
    },
    canUndo: () => undoStack.length > 0,
    canRedo: () => redoStack.length > 0,
    undoDepth: () => undoStack.length,
    redoDepth: () => redoStack.length,
    clear() {
      undoStack = [];
      redoStack = [];
    },
  };
}
