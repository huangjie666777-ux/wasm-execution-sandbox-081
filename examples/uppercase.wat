;; Data-conversion example: reads all bytes from stdin, converts ASCII
;; lowercase letters to uppercase on stdout, and writes a small summary to
;; stderr.  Pure WASI preview-1, no filesystem or network access.
(module
  (import "wasi_snapshot_preview1" "fd_write" (func $fd_write
    (param i32 i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "fd_read" (func $fd_read
    (param i32 i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "proc_exit" (func $proc_exit (param i32)))

  ;; Four 64 KiB pages.  Layout:
  ;;   0..60000       data buffer
  ;;   196608         iovec for read   (base, len)
  ;;   196616         iovec for stdout
  ;;   196624         iovec for stderr
  ;;   196632..       nwritten / nread scratch
  ;;   65000..        stderr marker string
  (memory (export "memory") 4)

  (func (export "_start")
    (local $nread i32) (local $i i32) (local $c i32) (local $total i32)

    ;; read iovec: { base=0, len=60000 } at address 65536
    (i32.store (i32.const 196608) (i32.const 0))
    (i32.store (i32.const 196612) (i32.const 60000))
    ;; write iovec stdout: { base=0, len=<filled later> } at 196616
    (i32.store (i32.const 196616) (i32.const 0))
    ;; stderr iovec at 196624, message at 65000
    (i32.store (i32.const 196624) (i32.const 65000))

    ;; fd_read(0, iov=65536, iovcnt=1, nread_ptr=65560)
    (drop (call $fd_read (i32.const 0) (i32.const 196608) (i32.const 1)
                         (i32.const 196632)))
    (local.set $nread (i32.load (i32.const 196632)))

    ;; Convert ASCII a-z -> A-Z in place.
    (local.set $i (i32.const 0))
    (block $done
      (loop $loop
        (br_if $done (i32.ge_u (local.get $i) (local.get $nread)))
        (local.set $c (i32.load8_u (local.get $i)))
        (if (i32.and (i32.ge_u (local.get $c) (i32.const 97))
                     (i32.le_u (local.get $c) (i32.const 122)))
          (then
            (i32.store8 (local.get $i)
                        (i32.sub (local.get $c) (i32.const 32)))))
        (local.set $i (i32.add (local.get $i) (i32.const 1)))
        (br $loop)))

    ;; fd_write(1, iov=65544, iovcnt=1, nwritten=65560)
    (i32.store (i32.const 196620) (local.get $nread))
    (drop (call $fd_write (i32.const 1) (i32.const 196616) (i32.const 1)
                          (i32.const 196632)))

    ;; A fixed stderr marker (placed at instantiation by the data segment).
    (i32.store (i32.const 196628) (i32.const 16))
    (drop (call $fd_write (i32.const 2) (i32.const 196624) (i32.const 1)
                          (i32.const 196632)))
  )

  (data (i32.const 65000) "uppercase: done\n")
)
