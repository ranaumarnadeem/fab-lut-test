// fabtest native: the search state of the test config router (patterns/config/fab_router.Cfg).
//
// Nets are ids 0..n-1 in sorted name order (ties in the Dijkstra queue then break as the
// Python (distance, net name) heap does); muxes are ids in Graph.ids order. The graph:
// fixed edges a -> b with a cost (inter-tile wires, pass-throughs, BEL edges), their
// predecessor lists, per net the (mux, code) that read it, per mux the source net of
// every code (-1: a constant). Forbidden codes are checked in Python (Cfg.assign). The state: the selects
// (sel), the observed nets (obs, with a save / restore stack for point-to-point routes),
// the cut nets (constant LUT outputs: no input -> output edge) and the open-work costs.
// Every method follows the Python one it replaces step for step (same visiting order).
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstdint>
#include <queue>
#include <utility>
#include <vector>

namespace py = pybind11;
using Edge = std::pair<int32_t, double>;
using Code = std::pair<int32_t, int32_t>;

class Router {
public:
    Router(int32_t n, std::vector<std::vector<Edge>> fixed, std::vector<std::vector<int32_t>> fixed_pred,
           std::vector<std::vector<Code>> readers, std::vector<int32_t> net_of, std::vector<int32_t> mux_of,
           std::vector<std::vector<int32_t>> src)
        : n_(n), fixed_(std::move(fixed)), fixed_pred_(std::move(fixed_pred)), readers_(std::move(readers)),
          net_of_(std::move(net_of)), mux_of_(std::move(mux_of)), src_(std::move(src)) {
        if ((int32_t)fixed_.size() != n_ || (int32_t)fixed_pred_.size() != n_ || (int32_t)readers_.size() != n_ ||
            (int32_t)mux_of_.size() != n_)
            throw py::value_error("Router: per-net tables must have n entries");
        m_ = (int32_t)net_of_.size();
        sel_.assign(m_, -1);
        obs_.assign(n_, 0);
        cut_.assign(n_, 0);
        open_mux_.assign(m_, 0);
        open_code_.resize(m_);
        for (int32_t i = 0; i < m_; ++i) open_code_[i].assign(src_[i].size(), 0);
        seen_.assign(n_, 0);
        dist_.assign(n_, kInf);
        prev_.assign(n_, Prev{-1, -1, -1});
    }

    // ---------------------------------------------------------------- state
    void reset() {   // a new config: nothing selected, observed, cut or open
        std::fill(sel_.begin(), sel_.end(), -1);
        std::fill(obs_.begin(), obs_.end(), 0);
        std::fill(cut_.begin(), cut_.end(), 0);
        std::fill(open_mux_.begin(), open_mux_.end(), 0);
        for (auto& c : open_code_) std::fill(c.begin(), c.end(), 0);
        saved_.clear();
        last_end_ = -1;
    }
    int32_t sel_get(int32_t i) const { return sel_.at(i); }
    void sel_set(int32_t i, int32_t k) { sel_.at(i) = k; }
    void sel_del(int32_t i) { sel_.at(i) = -1; }
    bool obs_has(int32_t v) const { return v >= 0 && v < n_ && obs_[v]; }
    void obs_add(int32_t v) { if (v >= 0 && v < n_) obs_[v] = 1; }
    void push_obs(const std::vector<int32_t>& targets) {
        saved_.push_back(obs_);
        obs_.assign(n_, 0);
        for (int32_t v : targets) if (v >= 0 && v < n_) obs_[v] = 1;
    }
    void pop_obs() {
        if (saved_.empty()) throw py::value_error("Router.pop_obs: nothing saved");
        obs_ = std::move(saved_.back());
        saved_.pop_back();
    }
    void set_cut(const std::vector<int32_t>& nets) {
        cut_.assign(n_, 0);
        for (int32_t v : nets) if (v >= 0 && v < n_) cut_[v] = 1;
    }
    void add_cut(int32_t v) { if (v >= 0 && v < n_) cut_[v] = 1; }
    void set_open(const std::vector<Code>& codes, const std::vector<int32_t>& muxes) {
        for (auto& c : open_code_) std::fill(c.begin(), c.end(), 0);
        std::fill(open_mux_.begin(), open_mux_.end(), 0);
        for (auto& [i, k] : codes) open_code_.at(i).at(k) = 1;
        for (int32_t i : muxes) open_mux_.at(i) = 1;
    }
    int32_t last_end() const { return last_end_; }

    // ---------------------------------------------------------------- searches
    // Cfg._mark: every predecessor of v (fixed, or the selected source of v's mux) observed
    void mark(int32_t v) {
        if (v < 0 || v >= n_) return;
        std::vector<int32_t> stack{v};
        while (!stack.empty()) {
            int32_t x = stack.back();
            stack.pop_back();
            if (!cut_[x])
                for (int32_t p : fixed_pred_[x])
                    if (!obs_[p]) { obs_[p] = 1; stack.push_back(p); }
            int32_t i = mux_of_[x];
            if (i >= 0 && sel_[i] >= 0) {
                int32_t p = src_[i][sel_[i]];
                if (p >= 0 && !obs_[p]) { obs_[p] = 1; stack.push_back(p); }
            }
        }
    }

    // Cfg.reaches: a reaches b over fixed edges (target not cut) and selected muxes
    bool reaches(int32_t a, int32_t b, int64_t cap) {
        if (a < 0 || a >= n_) return false;
        ++epoch_;
        seen_[a] = epoch_;
        int64_t nseen = 1;
        std::vector<int32_t> stack{a};
        while (!stack.empty()) {
            int32_t v = stack.back();
            stack.pop_back();
            if (v == b) return true;
            if (cap && nseen > cap) return false;
            for (auto& [t, c] : fixed_[v]) {
                (void)c;
                if (cut_[t] || seen_[t] == epoch_) continue;
                seen_[t] = epoch_; ++nseen; stack.push_back(t);
            }
            for (auto& [i, k] : readers_[v]) {
                if (sel_[i] != k) continue;
                int32_t t = net_of_[i];
                if (seen_[t] == epoch_) continue;
                seen_[t] = epoch_; ++nseen; stack.push_back(t);
            }
        }
        return false;
    }

    // Cfg.path_muxes: the selected muxes on a path a -> b (from b back to a), DFS order
    std::vector<int32_t> path_muxes(int32_t a, int32_t b) {
        std::vector<int32_t> out;
        if (a < 0 || a >= n_) return out;
        ++epoch_;
        seen_[a] = epoch_;
        std::vector<int32_t> stack{a};
        prev_[a].node = -1;
        while (!stack.empty()) {
            int32_t v = stack.back();
            stack.pop_back();
            if (v == b) {
                for (int32_t x = v; x >= 0; x = prev_[x].node) {
                    int32_t i = mux_of_[x];
                    if (i >= 0 && sel_[i] >= 0) out.push_back(i);
                    if (x == a) break;
                }
                return out;
            }
            auto visit = [&](int32_t t) {
                if (seen_[t] == epoch_) return;
                seen_[t] = epoch_; prev_[t].node = v; stack.push_back(t);
            };
            for (auto& [t, c] : fixed_[v]) { (void)c; if (!cut_[t]) visit(t); }
            for (auto& [i, k] : readers_[v]) if (sel_[i] == k) visit(net_of_[i]);
        }
        return out;
    }

    // Cfg.route: Dijkstra from start to the nearest observed net; None or the path's new
    // (mux, code) steps from start on. use_open: the open-work costs (set_open) apply
    py::object route(int32_t start, const std::vector<Code>& banned_list, int64_t cap, bool wires_only, bool use_open) {
        if (start < 0 || start >= n_) return py::none();
        for (int32_t v : touched_) { dist_[v] = kInf; prev_[v] = Prev{-1, -1, -1}; }
        touched_.clear();
        std::vector<char> banned_mux;   // banned is tiny: linear scan per mux
        using QE = std::pair<double, int32_t>;
        std::priority_queue<QE, std::vector<QE>, std::greater<QE>> pq;
        dist_[start] = 0.0; touched_.push_back(start);
        pq.push({0.0, start});
        int64_t pops = 0;
        while (!pq.empty()) {
            auto [d, v] = pq.top();
            pq.pop();
            if (d > dist_[v]) continue;
            ++pops;
            if (cap && pops > cap) return py::none();
            if (v != start && obs_[v]) {
                last_end_ = v;
                py::list path;
                std::vector<Code> rev;
                for (int32_t x = v; x != start; x = prev_[x].node)
                    if (prev_[x].mux >= 0) rev.push_back({prev_[x].mux, prev_[x].k});
                for (auto it = rev.rbegin(); it != rev.rend(); ++it) path.append(py::make_tuple(it->first, it->second));
                return std::move(path);
            }
            auto relax = [&](int32_t t, double c, int32_t mux, int32_t k) {
                double nd = d + c;
                if (nd < dist_[t]) {
                    if (dist_[t] == kInf) touched_.push_back(t);
                    dist_[t] = nd;
                    prev_[t] = Prev{v, mux, k};
                    pq.push({nd, t});
                }
            };
            for (auto& [t, c] : fixed_[v])
                if (!(wires_only && c > 0)) relax(t, c, -1, -1);
            for (auto& [i, k] : readers_[v]) {
                bool is_banned = false;
                for (auto& bk : banned_list) if (bk.first == i && bk.second == k) { is_banned = true; break; }
                if (is_banned) continue;
                if (sel_[i] >= 0) {
                    if (sel_[i] == k) relax(net_of_[i], 0.0, -1, -1);
                } else {
                    double c = 1.0;
                    if (use_open && open_code_[i][k]) c = 0.4;
                    else if (use_open && open_mux_[i]) c = 4.0;
                    relax(net_of_[i], c, i, k);
                }
            }
        }
        return py::none();
    }

private:
    struct Prev { int32_t node, mux, k; };
    static constexpr double kInf = 1e18;
    int32_t n_, m_;
    std::vector<std::vector<Edge>> fixed_;
    std::vector<std::vector<int32_t>> fixed_pred_;
    std::vector<std::vector<Code>> readers_;
    std::vector<int32_t> net_of_, mux_of_;
    std::vector<std::vector<int32_t>> src_;
    std::vector<int32_t> sel_;
    std::vector<char> obs_, cut_, open_mux_;
    std::vector<std::vector<char>> open_code_;
    std::vector<std::vector<char>> saved_;
    std::vector<uint32_t> seen_;
    uint32_t epoch_ = 0;
    std::vector<double> dist_;
    std::vector<Prev> prev_;
    std::vector<int32_t> touched_;
    int32_t last_end_ = -1;
};

void init_router(py::module_& m) {
    py::class_<Router>(m, "Router")
        .def(py::init<int32_t, std::vector<std::vector<Edge>>, std::vector<std::vector<int32_t>>,
                      std::vector<std::vector<Code>>, std::vector<int32_t>, std::vector<int32_t>,
                      std::vector<std::vector<int32_t>>>())
        .def("reset", &Router::reset)
        .def("sel_get", &Router::sel_get)
        .def("sel_set", &Router::sel_set)
        .def("sel_del", &Router::sel_del)
        .def("obs_has", &Router::obs_has)
        .def("obs_add", &Router::obs_add)
        .def("push_obs", &Router::push_obs)
        .def("pop_obs", &Router::pop_obs)
        .def("set_cut", &Router::set_cut)
        .def("add_cut", &Router::add_cut)
        .def("set_open", &Router::set_open)
        .def("last_end", &Router::last_end)
        .def("mark", &Router::mark)
        .def("reaches", &Router::reaches)
        .def("path_muxes", &Router::path_muxes)
        .def("route", &Router::route);
}
