/* Bit-parallel, 3-valued, event-driven fault simulator (one fault per lane).
 *
 * Every net holds two bitmasks per 64-lane word: H (value may be 1) and
 * L (value may be 0): 0 = (H 0, L 1), 1 = (1, 0), X = (1, 1). Lane 0 of
 * word 0 is the fault-free machine; every other lane carries one stuck-at
 * fault, applied as a mask whenever its net gets a new value (a Verilog
 * force that is never released).
 *
 * Cells: AND, OR, NOT, BUF (Verilog 3-valued semantics), the X-pessimistic
 * $_DLATCH_P_ and $_DFFE_PP_ models of sim/seq_models.v. Evaluation is
 * event driven: a cell is re-evaluated when one of its input nets changes,
 * in order of a topological rank of the cell graph (strongly connected
 * components, i.e. structural loops through the switch matrix, iterate to
 * their fixed point). Lanes that do not settle (a loop that oscillates under
 * the fault) are reported as timeouts and set to X.
 *
 * Time line as in the testbenches: inputs 0 and settle (t = 0); faults
 * applied and settle (t = 1 ns); then per step: apply inputs, settle,
 * compare the sampled outputs with lane 0.
 */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

enum { C_AND = 0, C_OR = 1, C_NOT = 2, C_BUF = 3, C_LATCH = 4, C_DFF = 5 };

typedef uint64_t u64;

typedef struct {
    int n_nets, n_cells, W;
    const int *type, *a, *b, *c, *out, *rank;   /* LATCH: a=E b=D; DFF: a=C b=E c=D */
    const int *rd_off, *rd_idx;                  /* readers of a net (cells) */
    const int *dff_slot;                         /* cell -> dff index or -1 */
    u64 *H, *L;                                  /* n_nets * W */
    u64 *CQH, *CQL;                              /* n_dff * W : previous clock value */
    int *fidx;                                   /* net -> fault mask slot or -1 */
    u64 *M0, *M1;                                /* slot * W */
    /* event queue (rank buckets) */
    int hn, *inq, *bhead, *bnext, minr, nr;
    u64 *bmap;
    u64 *dead;                                   /* lanes given up (oscillation): kept X */
    /* power-up enumeration (run_config_x): definitely-written tracking */
    const int *st_slot;                          /* cell -> storage slot or -1 (NULL: off) */
    u64 *wr;                                     /* n_store * W: lanes definitely written */
    int track;                                   /* tracking on (after the deposits, as the testbench) */
    /* local oscillation model (osc_local): only the nets that keep changing go X */
    u64 *oscx;                                   /* n_nets * W: lanes held at X (sticky) */
    u64 *netchg;                                 /* n_nets * W: lanes changed in the check round */
    int *touched, ntouched, rec;                 /* nets with netchg bits, recording on */
    u64 *osc;                                    /* W: lanes that oscillated (reported as T) */
} Sim;

/* 0: an oscillating lane is given up (X on every net); 1: only its oscillating
 * nets are held at X, the rest of the lane keeps being simulated. */
static int osc_local = 0;
void set_osc_local(int v) { osc_local = v; }

/* debugging: if set, run_impl copies the final H / L of every net (W = 1) */
static u64 *dump_h = NULL, *dump_l = NULL;
void set_dump(u64 *h, u64 *l) { dump_h = h; dump_l = l; }

/* Event queue: one bucket per topological rank, a bitmap of non-empty
 * buckets, and the lowest possibly non-empty rank. Push and pop are O(1)
 * apart from a word scan of the bitmap. Cells of one rank are popped LIFO. */
static inline void hpush(Sim *s, int cell) {
    if (s->inq[cell]) return;
    s->inq[cell] = 1;
    int r = s->rank[cell];
    s->bnext[cell] = s->bhead[r];
    s->bhead[r] = cell;
    s->bmap[r >> 6] |= (u64)1 << (r & 63);
    if (r < s->minr) s->minr = r;
    s->hn++;
}

static inline int hpop(Sim *s) {   /* caller ensures hn > 0 */
    int w = s->minr >> 6;
    u64 x = s->bmap[w] & (~(u64)0 << (s->minr & 63));
    while (!x) x = s->bmap[++w];
    int r = (w << 6) + __builtin_ctzll(x);
    s->minr = r;
    int cell = s->bhead[r];
    s->bhead[r] = s->bnext[cell];
    if (s->bhead[r] < 0) s->bmap[r >> 6] &= ~((u64)1 << (r & 63));
    s->hn--;
    s->inq[cell] = 0;
    return cell;
}

static inline void push_readers(Sim *s, int net) {
    for (int k = s->rd_off[net]; k < s->rd_off[net + 1]; k++) hpush(s, s->rd_idx[k]);
}

/* write a value into a net (with its fault masks); returns 1 if it changed */
static int set_net(Sim *s, int net, const u64 *h, const u64 *l, u64 *chg) {
    int W = s->W, f = s->fidx[net], any = 0;
    u64 *H = s->H + (size_t)net * W, *L = s->L + (size_t)net * W;
    for (int w = 0; w < W; w++) {
        u64 nh = h[w], nl = l[w];
        if (f >= 0) {
            u64 m0 = s->M0[(size_t)f * W + w], m1 = s->M1[(size_t)f * W + w];
            nh = (nh & ~m0) | m1;
            nl = (nl & ~m1) | m0;
        }
        nh |= s->dead[w];
        nl |= s->dead[w];
        if (s->oscx) { u64 x = s->oscx[(size_t)net * W + w]; nh |= x; nl |= x; }
        u64 d = (nh ^ H[w]) | (nl ^ L[w]);
        if (d) {
            any = 1; H[w] = nh; L[w] = nl; if (chg) chg[w] |= d;
            if (s->rec) {
                u64 *nc = s->netchg + (size_t)net * W;
                int fresh = 1;
                for (int v = 0; v < W; v++) if (nc[v]) { fresh = 0; break; }
                if (fresh) s->touched[s->ntouched++] = net;
                nc[w] |= d;
            }
        }
    }
    return any;
}

/* keep Q where Q == D is a defined value, else X */
static inline void keep_or_x(u64 qh, u64 ql, u64 dh, u64 dl, u64 *rh, u64 *rl) {
    u64 eq = (qh & ~ql & dh & ~dl) | (ql & ~qh & dl & ~dh);
    *rh = (qh & eq) | ~eq;      /* X where not equal */
    *rl = (ql & eq) | ~eq;
}

static void eval_cell(Sim *s, int ci, u64 *chg) {
    int W = s->W, t = s->type[ci];
    u64 nh[16], nl[16];          /* W <= 16 */
    const u64 *AH = s->H + (size_t)s->a[ci] * W, *AL = s->L + (size_t)s->a[ci] * W;
    switch (t) {
    case C_AND: {
        const u64 *BH = s->H + (size_t)s->b[ci] * W, *BL = s->L + (size_t)s->b[ci] * W;
        for (int w = 0; w < W; w++) { nh[w] = AH[w] & BH[w]; nl[w] = AL[w] | BL[w]; }
        break; }
    case C_OR: {
        const u64 *BH = s->H + (size_t)s->b[ci] * W, *BL = s->L + (size_t)s->b[ci] * W;
        for (int w = 0; w < W; w++) { nh[w] = AH[w] | BH[w]; nl[w] = AL[w] & BL[w]; }
        break; }
    case C_NOT:
        for (int w = 0; w < W; w++) { nh[w] = AL[w]; nl[w] = AH[w]; }
        break;
    case C_BUF:
        for (int w = 0; w < W; w++) { nh[w] = AH[w]; nl[w] = AL[w]; }
        break;
    case C_LATCH: {   /* a = E, b = D, out = Q */
        const u64 *DH = s->H + (size_t)s->b[ci] * W, *DL = s->L + (size_t)s->b[ci] * W;
        const u64 *QH = s->H + (size_t)s->out[ci] * W, *QL = s->L + (size_t)s->out[ci] * W;
        for (int w = 0; w < W; w++) {
            u64 e1 = AH[w] & ~AL[w], e0 = AL[w] & ~AH[w], ex = AH[w] & AL[w];
            u64 kh, kl; keep_or_x(QH[w], QL[w], DH[w], DL[w], &kh, &kl);
            nh[w] = (e1 & DH[w]) | (e0 & QH[w]) | (ex & kh);
            nl[w] = (e1 & DL[w]) | (e0 & QL[w]) | (ex & kl);
            if (s->track && s->st_slot[ci] >= 0) s->wr[(size_t)s->st_slot[ci] * W + w] |= e1;
        }
        break; }
    case C_DFF: {     /* a = C, b = E, c = D, out = Q */
        int k = s->dff_slot[ci];
        const u64 *EH = s->H + (size_t)s->b[ci] * W, *EL = s->L + (size_t)s->b[ci] * W;
        const u64 *DH = s->H + (size_t)s->c[ci] * W, *DL = s->L + (size_t)s->c[ci] * W;
        const u64 *QH = s->H + (size_t)s->out[ci] * W, *QL = s->L + (size_t)s->out[ci] * W;
        u64 *CQH = s->CQH + (size_t)k * W, *CQL = s->CQL + (size_t)k * W;
        for (int w = 0; w < W; w++) {
            u64 c1 = AH[w] & ~AL[w], cx = AH[w] & AL[w];
            u64 q0 = CQL[w] & ~CQH[w], qx = CQH[w] & CQL[w];
            u64 cchg = (AH[w] ^ CQH[w]) | (AL[w] ^ CQL[w]);   /* a clock event happened */
            u64 defpos = cchg & q0 & c1;
            u64 posspos = cchg & ((qx & c1) | (q0 & cx));
            u64 e1 = EH[w] & ~EL[w], e0 = EL[w] & ~EH[w];
            u64 kh, kl; keep_or_x(QH[w], QL[w], DH[w], DL[w], &kh, &kl);
            u64 take = defpos & e1;
            u64 kox = (defpos & ~e1 & ~e0) | (posspos & ~e0);
            u64 hold = ~(take | kox);
            nh[w] = (take & DH[w]) | (kox & kh) | (hold & QH[w]);
            nl[w] = (take & DL[w]) | (kox & kl) | (hold & QL[w]);
            CQH[w] = AH[w]; CQL[w] = AL[w];
            if (s->track && s->st_slot[ci] >= 0) s->wr[(size_t)s->st_slot[ci] * W + w] |= take;
        }
        break; }
    default:
        return;
    }
    if (set_net(s, s->out[ci], nh, nl, chg)) push_readers(s, s->out[ci]);
}

/* settle: returns 0 when every lane settled. Lanes that keep changing past
 * the limit are added to `dead` and set to X on every net. */
static void settle(Sim *s, u64 *dead, long limit) {
    int W = s->W;
    u64 chg[16];
    long pops = 0;
    int strikes = 0;   /* rounds in which the fault-free lane still changed */
    while (s->hn) {
        int ci = hpop(s);
        if (pops < limit) { eval_cell(s, ci, NULL); pops++; continue; }
        /* past the limit: watch which lanes still change for one more round */
        memset(chg, 0, sizeof chg);
        long extra = 0;
        eval_cell(s, ci, chg);
        while (s->hn && extra < 4L * s->n_cells) { eval_cell(s, hpop(s), chg); extra++; }
        if (!s->hn) return;   /* it did settle after all */
        if (osc_local) {
            /* record which nets still change, per lane, in one more round */
            for (int k = 0; k < s->ntouched; k++) memset(s->netchg + (size_t)s->touched[k] * W, 0, W * 8);
            s->ntouched = 0;
            s->rec = 1;
            long extra2 = 0;
            u64 c2[16]; memset(c2, 0, sizeof c2);
            while (s->hn && extra2 < 4L * s->n_cells) { eval_cell(s, hpop(s), c2); extra2++; }
            s->rec = 0;
            if (!s->hn) return;
            if (c2[0] & 1) {
                /* local mode needs more rounds for the faulty lanes to calm down
                 * (only their oscillating nets go X): allow the fault-free lane 10 */
                if (++strikes >= 10) { dead[0] |= 1; return; }
            }
            for (int k = 0; k < s->ntouched; k++) {
                int n = s->touched[k];
                u64 *nc = s->netchg + (size_t)n * W, h[16], l[16];
                int any = 0;
                for (int w = 0; w < W; w++) {
                    u64 m = nc[w] & ~(w == 0 ? (u64)1 : 0);
                    s->oscx[(size_t)n * W + w] |= m;
                    s->osc[w] |= m;
                    h[w] = s->H[(size_t)n * W + w] | m;
                    l[w] = s->L[(size_t)n * W + w] | m;
                    any |= m != 0;
                }
                if (any && set_net(s, n, h, l, NULL)) push_readers(s, n);
            }
            pops = 0;
            continue;
        }
        /* oscillating lanes: X everywhere, then settle again. The fault-free
         * lane can still be changing only because oscillating lanes starved
         * its events (rank order): it is never set to X; it counts as
         * oscillating when it still changes after 3 rounds. */
        if (chg[0] & 1) {
            if (++strikes >= 3) { dead[0] |= 1; return; }   /* caller reports it */
            chg[0] &= ~(u64)1;
        }
        for (int w = 0; w < W; w++) dead[w] |= chg[w];
        for (int n = 0; n < s->n_nets; n++)
            for (int w = 0; w < W; w++) {
                s->H[(size_t)n * W + w] |= chg[w];
                s->L[(size_t)n * W + w] |= chg[w];
            }
        for (int k = 0; k < s->n_cells; k++) hpush(s, k);
        pops = 0;
    }
}

/* One config (stimulus) for one batch of faults.
 *  in_nets[n_in]: input port nets; stim[n_steps * n_in] 0/1 bytes; sample[n_steps]
 *  out_nets[n_out]; fault lanes: lane k (1 .. n_faults) on fnet[k-1] with fpol[k-1]
 *  res[lane]: bit0 D, bit1 X seen at an output, bit2 timeout; golden written to
 *  gold[n_samples * n_out] (lane 0 values, 2 = X)
 * returns 0, or -1 if the fault-free lane oscillated.
 */
static int run_impl(int n_nets, int n_cells, int W,
               const int *type, const int *a, const int *b, const int *c, const int *out,
               const int *rank, const int *rd_off, const int *rd_idx, const int *dff_slot, int n_dff,
               const int *const_net, const int *const_val, int n_const,
               int n_in, const int *in_nets, int n_steps, const unsigned char *stim,
               const unsigned char *sample, int n_out, const int *out_nets,
               int n_faults, const int *fnet, const int *fpol,
               unsigned char *res, unsigned char *gold, long limit,
               int n_init, const int *init_net, const u64 *init_h, const u64 *init_l, const u64 *init_m,
               const int *st_slot, int n_store, const int *st_net, u64 *wr, u64 *stq_h, u64 *stq_l,
               u64 *detmap);

int run_config(int n_nets, int n_cells, int W,
               const int *type, const int *a, const int *b, const int *c, const int *out,
               const int *rank, const int *rd_off, const int *rd_idx, const int *dff_slot, int n_dff,
               const int *const_net, const int *const_val, int n_const,
               int n_in, const int *in_nets, int n_steps, const unsigned char *stim,
               const unsigned char *sample, int n_out, const int *out_nets,
               int n_faults, const int *fnet, const int *fpol,
               unsigned char *res, unsigned char *gold, long limit)
{
    return run_impl(n_nets, n_cells, W, type, a, b, c, out, rank, rd_off, rd_idx, dff_slot, n_dff,
                    const_net, const_val, n_const, n_in, in_nets, n_steps, stim, sample, n_out, out_nets,
                    n_faults, fnet, fpol, res, gold, limit, 0, NULL, NULL, NULL, NULL,
                    NULL, 0, NULL, NULL, NULL, NULL, NULL);
}

/* run_config plus power-up enumeration support:
 *  init_net[n_init] (storage Q nets), init_h / init_l / init_m [n_init * W]: deposited
 *    (value h / l on the lanes of m) after the faults settle, as the testbench's
 *    +PU deposits; tracking starts after them;
 *  st_slot[n_cells] (cell -> storage slot or -1), st_net[n_store] (slot -> Q net):
 *    wr[n_store * W] lanes in which the element was definitely written (latch
 *    enable exactly 1; flop clean 0 -> 1 clock edge with enable 1), stq_h / stq_l
 *    [n_store * W] its value at the end;
 *  detmap[n_out * W]: lanes with a definite mismatch against lane 0 at output o
 *    (any sample; detected lanes keep accumulating). */
int run_config_x(int n_nets, int n_cells, int W,
               const int *type, const int *a, const int *b, const int *c, const int *out,
               const int *rank, const int *rd_off, const int *rd_idx, const int *dff_slot, int n_dff,
               const int *const_net, const int *const_val, int n_const,
               int n_in, const int *in_nets, int n_steps, const unsigned char *stim,
               const unsigned char *sample, int n_out, const int *out_nets,
               int n_faults, const int *fnet, const int *fpol,
               unsigned char *res, unsigned char *gold, long limit,
               int n_init, const int *init_net, const u64 *init_h, const u64 *init_l, const u64 *init_m,
               const int *st_slot, int n_store, const int *st_net, u64 *wr, u64 *stq_h, u64 *stq_l,
               u64 *detmap)
{
    return run_impl(n_nets, n_cells, W, type, a, b, c, out, rank, rd_off, rd_idx, dff_slot, n_dff,
                    const_net, const_val, n_const, n_in, in_nets, n_steps, stim, sample, n_out, out_nets,
                    n_faults, fnet, fpol, res, gold, limit, n_init, init_net, init_h, init_l, init_m,
                    st_slot, n_store, st_net, wr, stq_h, stq_l, detmap);
}

static int run_impl(int n_nets, int n_cells, int W,
               const int *type, const int *a, const int *b, const int *c, const int *out,
               const int *rank, const int *rd_off, const int *rd_idx, const int *dff_slot, int n_dff,
               const int *const_net, const int *const_val, int n_const,
               int n_in, const int *in_nets, int n_steps, const unsigned char *stim,
               const unsigned char *sample, int n_out, const int *out_nets,
               int n_faults, const int *fnet, const int *fpol,
               unsigned char *res, unsigned char *gold, long limit,
               int n_init, const int *init_net, const u64 *init_h, const u64 *init_l, const u64 *init_m,
               const int *st_slot, int n_store, const int *st_net, u64 *wr, u64 *stq_h, u64 *stq_l,
               u64 *detmap)
{
    Sim s;
    memset(&s, 0, sizeof s);
    s.n_nets = n_nets; s.n_cells = n_cells; s.W = W;
    s.type = type; s.a = a; s.b = b; s.c = c; s.out = out; s.rank = rank;
    s.rd_off = rd_off; s.rd_idx = rd_idx; s.dff_slot = dff_slot;
    s.H = malloc((size_t)n_nets * W * 8); s.L = malloc((size_t)n_nets * W * 8);
    s.CQH = malloc((size_t)(n_dff ? n_dff : 1) * W * 8); s.CQL = malloc((size_t)(n_dff ? n_dff : 1) * W * 8);
    s.fidx = malloc((size_t)n_nets * sizeof(int));
    s.M0 = calloc((size_t)(n_faults + 1) * W, 8); s.M1 = calloc((size_t)(n_faults + 1) * W, 8);
    s.inq = calloc(n_cells, sizeof(int));
    s.nr = 0;
    for (int k = 0; k < n_cells; k++) if (rank[k] + 1 > s.nr) s.nr = rank[k] + 1;
    s.bhead = malloc((size_t)s.nr * sizeof(int));
    for (int k = 0; k < s.nr; k++) s.bhead[k] = -1;
    s.bnext = malloc((size_t)n_cells * sizeof(int));
    s.bmap = calloc((size_t)(s.nr >> 6) + 2, 8);
    s.minr = 0;
    int lanes = 64 * W;
    u64 allX = ~(u64)0;
    for (size_t i = 0; i < (size_t)n_nets * W; i++) { s.H[i] = allX; s.L[i] = allX; }
    for (size_t i = 0; i < (size_t)(n_dff ? n_dff : 1) * W; i++) { s.CQH[i] = allX; s.CQL[i] = allX; }
    for (int n = 0; n < n_nets; n++) s.fidx[n] = -1;
    u64 ones[16], zeros[16], dead[16];
    for (int w = 0; w < W; w++) { ones[w] = allX; zeros[w] = 0; dead[w] = 0; }
    s.dead = dead;
    u64 osc[16]; memset(osc, 0, sizeof osc);
    s.osc = osc;
    if (osc_local) {
        s.oscx = calloc((size_t)n_nets * W, 8);
        s.netchg = calloc((size_t)n_nets * W, 8);
        s.touched = malloc((size_t)n_nets * sizeof(int));
    }
    /* constants */
    for (int k = 0; k < n_const; k++) {
        int n = const_net[k], v = const_val[k];
        for (int w = 0; w < W; w++) {
            s.H[(size_t)n * W + w] = (v == 1 || v == 2) ? allX : 0;
            s.L[(size_t)n * W + w] = (v == 0 || v == 2) ? allX : 0;
        }
    }
    /* t = 0: inputs 0, everything evaluated once */
    for (int i = 0; i < n_in; i++) set_net(&s, in_nets[i], zeros, ones, NULL);
    for (int k = 0; k < n_cells; k++) hpush(&s, k);
    settle(&s, dead, limit);
    /* t = 1 ns: faults */
    int nslot = 0;
    for (int k = 0; k < n_faults && k + 1 < lanes; k++) {
        int lane = k + 1, w = lane >> 6, bit = lane & 63, n = fnet[k];
        if (s.fidx[n] < 0) s.fidx[n] = nslot++;
        u64 *m = fpol[k] ? s.M1 : s.M0;
        m[(size_t)s.fidx[n] * W + w] |= (u64)1 << bit;
    }
    for (int n = 0; n < n_nets; n++) {
        if (s.fidx[n] < 0) continue;
        u64 h[16], l[16];
        memcpy(h, s.H + (size_t)n * W, W * 8); memcpy(l, s.L + (size_t)n * W, W * 8);
        if (set_net(&s, n, h, l, NULL)) push_readers(&s, n);
    }
    settle(&s, dead, limit);
    /* power-up deposits, then tracking */
    for (int k = 0; k < n_init; k++) {
        int n = init_net[k];
        u64 h[16], l[16];
        for (int w = 0; w < W; w++) {
            u64 m = init_m[(size_t)k * W + w];
            h[w] = (s.H[(size_t)n * W + w] & ~m) | (init_h[(size_t)k * W + w] & m);
            l[w] = (s.L[(size_t)n * W + w] & ~m) | (init_l[(size_t)k * W + w] & m);
        }
        if (set_net(&s, n, h, l, NULL)) push_readers(&s, n);
    }
    if (n_init) settle(&s, dead, limit);
    if (st_slot) {
        s.st_slot = st_slot; s.wr = wr; s.track = 1;
        memset(wr, 0, (size_t)n_store * W * 8);
    }
    if (detmap) memset(detmap, 0, (size_t)n_out * W * 8);
    /* steps */
    u64 det[16], xs[16];
    for (int w = 0; w < W; w++) { det[w] = 0; xs[w] = 0; }
    const unsigned char *prev = NULL;
    int ns = 0, rc = 0;
    for (int t = 0; t < n_steps; t++) {
        const unsigned char *row = stim + (size_t)t * n_in;
        for (int i = 0; i < n_in; i++) {
            if (prev && prev[i] == row[i]) continue;
            if (set_net(&s, in_nets[i], row[i] ? ones : zeros, row[i] ? zeros : ones, NULL))
                push_readers(&s, in_nets[i]);
        }
        prev = row;
        settle(&s, dead, limit);
        if (dead[0] & 1) { rc = -1; break; }
        if (sample[t]) {
            for (int o = 0; o < n_out; o++) {
                const u64 *H = s.H + (size_t)out_nets[o] * W, *L = s.L + (size_t)out_nets[o] * W;
                int g1 = (H[0] & 1) && !(L[0] & 1), g0 = (L[0] & 1) && !(H[0] & 1);
                gold[(size_t)ns * n_out + o] = g1 ? 1 : g0 ? 0 : 2;
                for (int w = 0; w < W; w++) {
                    u64 live = ~dead[w] & ~det[w];
                    u64 d = g1 ? (L[w] & ~H[w]) : g0 ? (H[w] & ~L[w]) : 0;
                    det[w] |= d & live;
                    xs[w] |= (H[w] & L[w]) & live;
                    if (detmap) detmap[(size_t)o * W + w] |= d & ~dead[w];
                }
            }
            ns++;
        }
    }
    for (int lane = 0; lane < lanes; lane++) {
        int w = lane >> 6; u64 bit = (u64)1 << (lane & 63);
        res[lane] = ((det[w] & bit) ? 1 : 0) | ((xs[w] & bit) ? 2 : 0) | (((dead[w] | osc[w]) & bit) ? 4 : 0);
    }
    if (dump_h && W == 1) { memcpy(dump_h, s.H, (size_t)n_nets * 8); memcpy(dump_l, s.L, (size_t)n_nets * 8); }
    if (stq_h)
        for (int k = 0; k < n_store; k++)
            for (int w = 0; w < W; w++) {
                stq_h[(size_t)k * W + w] = s.H[(size_t)st_net[k] * W + w];
                stq_l[(size_t)k * W + w] = s.L[(size_t)st_net[k] * W + w];
            }
    free(s.H); free(s.L); free(s.CQH); free(s.CQL); free(s.fidx); free(s.M0); free(s.M1);
    free(s.inq); free(s.bhead); free(s.bnext); free(s.bmap);
    free(s.oscx); free(s.netchg); free(s.touched);
    return rc;
}
