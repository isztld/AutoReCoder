# AutoReCoder — Migration Pattern Library

This file is maintained by the agent. Updated after every experiment.
Format: one section per pattern. The agent adds new patterns and updates success rates.

---

## How to Read This File

Each pattern entry:
- **C idiom**: what the original C code looks like
- **Rust safe equivalent**: the safe replacement
- **Success rate**: `successes / attempts` (updated by agent)
- **Common failures**: why this pattern sometimes doesn't work
- **Notes**: anything learned from experiments

Success rate guide:
- `> 0.7` → prefer this pattern when applicable
- `0.4–0.7` → try this first, but have a backup plan
- `< 0.4` → attempt only if no alternative exists
- `[UNRELIABLE]` → failed 3+ consecutive times, skip and report to human

---

## Pattern: redundant-inner-unsafe

**C idiom:**
Any function where c2rust wraps the entire body in `unsafe {}` even though no unsafe operations
are present (e.g., pure arithmetic, integer comparisons, constant operations).

**Rust safe equivalent:**
```rust
// Before (c2rust output):
pub unsafe extern "C" fn foo(x: i32) -> i32 {
    unsafe {
        return x + 1;  // no unsafe operations here!
    }
}

// After (remove inner unsafe block):
pub unsafe extern "C" fn foo(x: i32) -> i32 {
    x + 1  // same semantics, one less unsafe block
}
```

Note: The outer `unsafe extern "C"` is IRREDUCIBLE (required for C ABI). Only the inner `unsafe {}` is removed.

**Success rate:** 1/1
**Common failures:** (none yet)
**Notes:** First confirmed working pattern. c2rust blanket-wraps function bodies in `unsafe {}` even
when the body contains no unsafe operations. Simple to detect: look for `unsafe {}` blocks where
the body contains no raw pointer ops, transmutes, extern calls, or asm!.

---

## Pattern: raw-pointer-array-access

**C idiom:**
```c
T *ptr = ...;
int n = ...;
ptr[i] = value;   // or: *(ptr + i)
```

**Rust safe equivalent:**
```rust
// Option A: restructure caller to pass slice
fn foo(slice: &mut [T]) {
    slice[i] = value;  // panics on OOB instead of UB
}

// Option B: if length known, create slice from raw parts (still unsafe, but isolated)
let slice = unsafe { std::slice::from_raw_parts_mut(ptr, n) };
slice[i] = value;  // reduces unsafe surface but doesn't eliminate it
```

**Prefer Option A** — eliminates unsafe entirely. Option B reduces surface but keeps unsafe.

**Success rate:** 0/0
**Common failures:** (none yet)
**Notes:** (none yet)

---

## Pattern: malloc-free-pair

**C idiom:**
```c
T *p = malloc(sizeof(T));
// ... use p ...
free(p);
```

**Rust safe equivalent:**
```rust
let p = Box::new(T::default());
// ... use p ...
// dropped automatically
```

For arrays:
```c
T *arr = malloc(n * sizeof(T));
free(arr);
```
```rust
let arr: Vec<T> = vec![T::default(); n];
// dropped automatically
```

**Success rate:** 0/0
**Common failures:** malloc result used as both owned and aliased pointer (common in C, illegal in Rust ownership model)
**Notes:** (none yet)

---

## Pattern: manual-string-buffer

**C idiom:**
```c
char buf[256];
snprintf(buf, sizeof(buf), "hello %s", name);
```

**Rust safe equivalent:**
```rust
let buf = format!("hello {}", name);
// or if fixed-size buffer semantics needed:
use std::fmt::Write;
let mut buf = String::with_capacity(256);
write!(&mut buf, "hello {}", name).unwrap();
```

**Success rate:** 0/0
**Common failures:** (none yet)
**Notes:** (none yet)

---

## Pattern: linked-list-node

**C idiom:**
```c
typedef struct Node {
    int value;
    struct Node *next;
} Node;

Node *new_node(int v) {
    Node *n = malloc(sizeof(Node));
    n->value = v;
    n->next = NULL;
    return n;
}
```

**Rust safe equivalent:**
```rust
struct Node {
    value: i32,
    next: Option<Box<Node>>,
}

impl Node {
    fn new(v: i32) -> Box<Self> {
        Box::new(Node { value: v, next: None })
    }
}
```

**Success rate:** 0/0
**Common failures:** back-pointers (parent/prev) cause ownership cycles — Rust disallows these without `Rc<RefCell<>>` or index-based approaches
**Notes:** If the C struct has a back-pointer, this pattern fails. Use index-based arena approach instead.

---

## Pattern: global-mutable-state

**C idiom:**
```c
static int g_counter = 0;
void increment() { g_counter++; }
int get() { return g_counter; }
```

**Rust safe equivalent:**
```rust
use std::sync::atomic::{AtomicI32, Ordering};
static G_COUNTER: AtomicI32 = AtomicI32::new(0);

fn increment() { G_COUNTER.fetch_add(1, Ordering::Relaxed); }
fn get() -> i32 { G_COUNTER.load(Ordering::Relaxed) }
```

For complex types:
```rust
use std::sync::{Mutex, OnceLock};
static G_STATE: OnceLock<Mutex<State>> = OnceLock::new();
fn get_state() -> &'static Mutex<State> {
    G_STATE.get_or_init(|| Mutex::new(State::default()))
}
```

**Success rate:** 0/0
**Common failures:** complex structs with non-Send types, state that is read-modified-write non-atomically
**Notes:** (none yet)

---

## Pattern: transmute-cast

**C idiom:**
```c
float f = 3.14;
uint32_t bits = *(uint32_t*)&f;  // type punning
```

**Rust safe equivalent:**
```rust
let f: f32 = 3.14;
let bits: u32 = f.to_bits();  // safe, same semantics
```

For more complex casts:
```rust
// If no safe equivalent: use bytemuck crate (Pod types only)
// NEVER use std::mem::transmute as a workaround
```

**Success rate:** 0/0
**Common failures:** transmute between non-Pod types, aliased pointers
**Notes:** `f32::to_bits()`, `u32::from_be_bytes()`, `bytemuck::cast()` cover most legitimate type-punning cases

---

## Pattern: arena-allocator

**C idiom:**
```c
typedef struct Arena { char *buf; size_t pos; size_t cap; } Arena;
void *arena_alloc(Arena *a, size_t sz) {
    void *p = a->buf + a->pos;
    a->pos += sz;
    return p;
}
```

**Rust safe equivalent:**
```rust
// Option A: bumpalo crate (if allowed)
use bumpalo::Bump;
let bump = Bump::new();
let p = bump.alloc(MyType::default());

// Option B: rewrite callers to use Vec<T> and indices instead of raw pointers
```

**Success rate:** 0/0
**Common failures:** bumpalo is an external crate (requires human approval to add); index-based rewrite is large scope change
**Notes:** Flag this pattern to human before attempting — likely requires adding bumpalo to Cargo.toml

---

## Deferred / Irreducible Patterns

Functions where unsafe cannot be eliminated — mark with `// IRREDUCIBLE UNSAFE: <reason>` and skip:

| Reason | Example |
|---|---|
| FFI boundary | `extern "C"` functions called from C code |
| Hardware/OS interface | `ioctl`, `mmap`, memory-mapped registers |
| Inline assembly | `asm!` blocks |
| Signal handlers | `libc::signal()` callbacks |
| Custom allocator impl | The allocator itself must use raw memory |
