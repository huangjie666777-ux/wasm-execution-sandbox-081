;; Keeps growing linear memory until stopped by the memory budget or fuel.
(module
  (import "wasi_snapshot_preview1" "proc_exit" (func $proc_exit (param i32)))
  (memory (export "memory") 1)
  (func (export "_start")
    (loop $grow
      ;; grow by 64 KiB pages; memory.grow returns -1 at the cap, then exit 7
      (if (i32.eqz (memory.grow (i32.const 1)))
        (then (call $proc_exit (i32.const 7))))
      ;; Touch the new last page so the allocation is genuinely exercised.
      (i32.store8 (i32.const 65535) (i32.const 1))
      (br $grow))))
