// Deprecated shim. The DICOM-only uploader has been replaced by the
// universal uploader in ./UniversalUploader.tsx. This file survives only
// so external callers importing the old path keep building until they
// migrate. Delete once no imports remain in the tree.

export { default } from "./UniversalUploader";
