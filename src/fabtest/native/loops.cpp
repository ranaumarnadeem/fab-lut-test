// fabtest native: combinational loop check of a fabric config (model/fabric.break_loops).
//
// LoopGraph holds the dependency graph of all nets once (node = net, succ = the nets its
// value depends on, in the model's order); a loop-repair move changes the dependency of
// one mux output (set_succ). first_cycle() runs the same iterative Tarjan as
// sim/graph.tarjan (roots in index order, successors in list order) and returns the first
// strongly connected component that is a loop (more than one node, or a self loop), so
// the Python and native paths pick the same loop and make the same move.
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstdint>
#include <vector>

namespace py = pybind11;

class LoopGraph {
public:
    explicit LoopGraph(const std::vector<std::vector<int32_t>>& succ) : succ_(succ) {
        n_ = static_cast<int32_t>(succ_.size());
        for (auto& s : succ_)
            for (int32_t w : s)
                if (w < 0 || w >= n_) throw py::value_error("LoopGraph: successor out of range");
    }

    void set_succ(int32_t v, const std::vector<int32_t>& s) {
        if (v < 0 || v >= n_) throw py::value_error("LoopGraph.set_succ: node out of range");
        for (int32_t w : s)
            if (w < 0 || w >= n_) throw py::value_error("LoopGraph.set_succ: successor out of range");
        succ_[v] = s;
    }

    int32_t size() const { return n_; }

    // first loop SCC in Tarjan emission order, empty if the graph is loop-free
    std::vector<int32_t> first_cycle() const {
        std::vector<int32_t> index(n_, -1), low(n_, 0);
        std::vector<char> on(n_, 0);
        std::vector<int32_t> stack;
        struct Frame { int32_t v; size_t it; };
        std::vector<Frame> work;
        int32_t counter = 0;
        for (int32_t root = 0; root < n_; ++root) {
            if (index[root] >= 0) continue;
            work.push_back({root, 0});
            index[root] = low[root] = counter++;
            stack.push_back(root);
            on[root] = 1;
            while (!work.empty()) {
                Frame& f = work.back();
                int32_t v = f.v;
                const auto& sv = succ_[v];
                bool advanced = false;
                while (f.it < sv.size()) {
                    int32_t w = sv[f.it++];
                    if (index[w] < 0) {
                        index[w] = low[w] = counter++;
                        stack.push_back(w);
                        on[w] = 1;
                        work.push_back({w, 0});   // f may dangle after this: not used again
                        advanced = true;
                        break;
                    }
                    if (on[w] && index[w] < low[v]) low[v] = index[w];
                }
                if (advanced) continue;
                work.pop_back();
                if (!work.empty()) {
                    int32_t u = work.back().v;
                    if (low[v] < low[u]) low[u] = low[v];
                }
                if (low[v] == index[v]) {
                    std::vector<int32_t> comp;
                    while (true) {
                        int32_t w = stack.back();
                        stack.pop_back();
                        on[w] = 0;
                        comp.push_back(w);
                        if (w == v) break;
                    }
                    if (comp.size() > 1) return comp;
                    for (int32_t w : succ_[v])
                        if (w == v) return comp;
                }
            }
        }
        return {};
    }

private:
    std::vector<std::vector<int32_t>> succ_;
    int32_t n_ = 0;
};

PYBIND11_MODULE(_native, m) {
    m.doc() = "fabtest native routines (loop check, router search)";
    py::class_<LoopGraph>(m, "LoopGraph")
        .def(py::init<const std::vector<std::vector<int32_t>>&>())
        .def("set_succ", &LoopGraph::set_succ)
        .def("first_cycle", &LoopGraph::first_cycle)
        .def("__len__", &LoopGraph::size);
}
