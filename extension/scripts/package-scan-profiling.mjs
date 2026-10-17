import {secretLintProfiler} from '@secretlint/profiler';

// Secretlint 10.2.2 always records timings and linearly searches every previous
// mark. A complete Lean runtime produces millions of marks and quadratic work.
// VSCE does not consume these timings. Keep every scanner and rule unchanged;
// suppress only the optional profiler in this packaging process.
secretLintProfiler.mark = () => {};
