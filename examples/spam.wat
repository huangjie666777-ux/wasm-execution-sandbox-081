;; Writes forever to stdout: output byte cap must stop it for real.
(module
  (import "wasi_snapshot_preview1" "fd_write" (func $fd_write
    (param i32 i32 i32 i32) (result i32)))
  (memory (export "memory") 1)
  (data (i32.const 0) "data\n")
  (func (export "_start")
    (i32.store (i32.const 1024) (i32.const 0))
    (i32.store (i32.const 1028) (i32.const 5))
    (loop $forever
      (drop (call $fd_write (i32.const 1) (i32.const 1024) (i32.const 1)
                            (i32.const 1040)))
      (br $forever))))
