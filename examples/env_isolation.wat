;; Calls environ_sizes_get.  An isolated guest has zero environment entries:
;; writes "ENV_OK" when both counts are 0, "ENV_LEAK" otherwise.
(module
  (import "wasi_snapshot_preview1" "environ_sizes_get"
    (func $environ_sizes_get (param i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "fd_write" (func $fd_write
    (param i32 i32 i32 i32) (result i32)))
  (memory (export "memory") 1)
  (data (i32.const 0) "ENV_OK\n")
  (data (i32.const 16) "ENV_LEAK\n")
  (func (export "_start")
    ;; counts at 1024 (count) and 1028 (buf size); iovec at 2048
    (drop (call $environ_sizes_get (i32.const 1024) (i32.const 1028)))
    (i32.store (i32.const 2052) (i32.const 7))
    (if (i32.or (i32.load (i32.const 1024))
                (i32.load (i32.const 1028)))
      (then (i32.store (i32.const 2048) (i32.const 16))
            (i32.store (i32.const 2052) (i32.const 9)))
      (else (i32.store (i32.const 2048) (i32.const 0))))
    (drop (call $fd_write (i32.const 1) (i32.const 2048) (i32.const 1)
                          (i32.const 3000)))
  )
)
