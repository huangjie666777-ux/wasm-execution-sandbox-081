;; Imports a host function we deliberately do not provide.
(module
  (import "env" "do_evil" (func $evil))
  (func (export "_start") (call $evil)))
