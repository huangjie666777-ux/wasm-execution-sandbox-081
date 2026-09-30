;; Deliberately traps with unreachable (nonzero abort-style failure).
(module
  (func (export "_start") unreachable))
