import numpy as np
import scipy.stats

from collections import defaultdict
from multiprocessing import Pool
import multiprocessing

import sys, os
try:
    sghome = os.environ['SIGVISA_HOME']
    if sghome not in sys.path:
        sys.path.append(sghome)
except:
    pass

from treegp.gp import GP, GPCov, mcov,  prior_sample

from treegp.cover_tree import VectorTree
import pyublas
from gpy_linalg import pdinv, dpotrs

def check_inv(prec, K):
    if K.shape[0] < 2:
        return 0.0

    k1 = np.abs(np.dot(prec[0, :], K[:, 0]) - 1)
    k2 = np.abs(np.dot(prec[0, :], K[:, 1]))
    k3 = np.abs(np.dot(prec[1, :], K[:, 0]))
    k4 = np.abs(np.dot(prec[1, :], K[:, 1]) - 1)
    numerical_error = np.max((k1, k2, k3, k4))
    return numerical_error

class Blocker(object):

    def __init__(self, block_centers):
        self.block_centers = block_centers
        self.n_blocks = len(block_centers)

    def get_block(self, X_new):
        dists = [np.linalg.norm(X_new - center) for center in self.block_centers]
        return np.argmin(dists)

    def block_clusters(self, X):
        dists = pair_distances(X, self.block_centers)
        blocks = np.argmin(dists, axis=1)
        idxs = []
        all_idxs = np.arange(len(X))
        for i in range(self.n_blocks):
            block_idxs = all_idxs[blocks == i]
            idxs.append(block_idxs)

        return idxs

    def neighbors(self, diag_connections=True):
        neighbors = []

        if len(self.block_centers) <= 1:
            return []

        center_distances = pair_distances(self.block_centers, self.block_centers)
        cc = center_distances.flatten()
        cc = cc[cc > 0]
        min_dist = np.min(cc) + 1e-6
        diag_dist = np.min(cc[cc > min_dist]) + 1e-6
        connect_dist = diag_dist if diag_connections else min_dist

        for i in range(self.n_blocks):
            for j in range(i):
                if center_distances[i,j] < connect_dist:
                    neighbors.append((i,j))
        return neighbors

def pair_distances(Xi, Xj):
    return np.sqrt(np.outer(np.sum(Xi**2, axis=1), np.ones((Xj.shape[0]),)) - 2*np.dot(Xi, Xj.T) + np.outer((np.ones(Xi.shape[0]),), np.sum(Xj**2, axis=1)))

def symmetrize_neighbors(neighbors):
    ndict = defaultdict(set)
    for (i, j) in neighbors:
        ndict[i].add(j)
        ndict[j].add(i)
    return ndict

class GPRF(object):

    def __init__(self, X, Y, block_fn, cov, noise_var, kernelized=False, dy=None,
                 neighbor_threshold=1e-3, nonstationary=False, nonstationary_prec=False,
                 block_idxs=None, neighbors=None):
        self.X = X

        if kernelized:
            self.kernelized = True
            self.YY = Y
            assert(dy is not None)
            self.dy = dy
        else:
            self.kernelized = False
            self.Y = Y
        if block_idxs is None:
            block_idxs = block_fn(X)
        self.block_idxs = block_idxs
        self.block_fn = block_fn
        self.n_blocks = len(block_idxs)

        self.nonstationary = nonstationary
        if not nonstationary:
            self.cov = cov
            self.noise_var = noise_var
            dummy_X = np.array([[0.0,] * self.X.shape[1],], dtype=float)
            self.predict_tree = VectorTree(dummy_X, 1, cov.dfn_str, cov.dfn_params, cov.wfn_str, cov.wfn_params)
        else:
            dummy_X = np.array([[0.0,] * self.X.shape[1],], dtype=float)
            predict_tree = VectorTree(dummy_X, 1, cov.dfn_str, cov.dfn_params, cov.wfn_str, cov.wfn_params)

            self.block_covs = [(noise_var, cov) for i in range(self.n_blocks)]
            self.block_trees = [predict_tree for i in range(self.n_blocks)]
            self.nonstationary_prec=nonstationary_prec

        if neighbors is not None:
            self.neighbors = neighbors
        else:
            self.compute_neighbors(threshold=neighbor_threshold)
        self.compute_neighbor_count()
        self.neighbor_dict = symmetrize_neighbors(self.neighbors)
        self.neighbor_threshold = neighbor_threshold

    def compute_neighbors(self, threshold=1e-3):

        neighbors = []

        if threshold == 1.0:
            self.neighbors = neighbors
            return

        wfn_var = self.cov.wfn_params[0]

        for i in range(self.n_blocks):
            print "computing neighbors for block", i
            idxs = self.block_idxs[i]
            #i_start, i_end = self.block_boundaries[i]
            X1 = self.X[idxs]
            for j in range(i):
                #j_start, j_end = self.block_boundaries[j]
                jdxs = self.block_idxs[j]
                X2 = self.X[jdxs]

                Kij = self.kernel(X1, X2=X2)/wfn_var
                maxk = np.max(np.abs(Kij))

                if maxk > threshold:
                    neighbors.append((i,j))

        self.neighbors = neighbors
        print "total pairs %d" % len(self.neighbors)

    def compute_neighbor_count(self):
        neighbor_count = defaultdict(int)
        for (i,j) in self.neighbors:
            neighbor_count[i] += 1
            neighbor_count[j] += 1
        self.neighbor_count = neighbor_count
                

    def update_covs(self, covs):
        if not self.nonstationary:
            nv, sv = covs[0, :2]
            lscales = covs[0, 2:]
            self.cov = GPCov(wfn_params=[sv,], dfn_params=lscales, dfn_str=self.cov.dfn_str, wfn_str=self.cov.wfn_str)
            self.noise_var = nv

            dummy_X = np.array([[0.0,] * self.X.shape[1],], dtype=float)
            self.predict_tree = VectorTree(dummy_X, 1, self.cov.dfn_str, self.cov.dfn_params, self.cov.wfn_str, self.cov.wfn_params)
        else:
            block_covs = []
            block_trees = []
            dummy_X = np.array([[0.0,] * self.X.shape[1],], dtype=float)
            for block, covparams in enumerate(covs):
                nv, sv = covparams[:2]
                lscales = covparams[2:]
                cov = GPCov(wfn_params=[sv,], dfn_params=lscales, dfn_str=self.dfn_str, wfn_str="se")
                block_covs.append((nv, cov))
                pt = VectorTree(dummy_X, 1, cov.dfn_str, cov.dfn_params, cov.wfn_str, cov.wfn_params)
                block_trees.append(pt)
            self.block_trees = block_trees
            self.block_covs = block_covs

    def update_X(self, new_X, update_blocks=True, recompute_neighbors=False):
        self.X = new_X
        if self.block_fn is not None:
            self.block_idxs = self.block_fn(new_X)
        if recompute_neighbors:
            self.compute_neighbors(threshold=self.neighbor_threshold)

    def update_X_block(self, i, new_X):
        #i_start, i_end = self.block_boundaries[i]
        idxs = self.block_idxs[i]
        self.X[idxs] = new_X

    def llgrad(self, parallel=False, local=True, **kwargs):
        # overall likelihood is the pairwise potentials for all (unordered) pairs,
        # where each block is involved in (n-1) pairs. So we subtract each unary potential n-1 times.
        # Then finally each unary potential gets added in once.

        if local:
            neighbors = self.neighbors
            neighbor_count = self.neighbor_count
        else:
            neighbors = [(i,j) for i in range(self.n_blocks) for j in range(i)]
            neighbor_count = dict([(i, self.n_blocks-1) for i in range(self.n_blocks)])

        if parallel:
            pool = Pool(processes=multiprocessing.cpu_count())
            try:
                unary_args = [(kwargs, self, i) for i in range(self.n_blocks)]
                unaries = pool.map_async(llgrad_unary_shim, unary_args).get(9999999)

                pair_args = [(kwargs, self, i, j) for (i,j) in neighbors]
                if len(pair_args) > 0:
                    pairs = pool.map_async(llgrad_joint_shim, pair_args).get(9999999)
                else:
                    pairs= []
                pool.close()
                pool.join()
            except KeyboardInterrupt:
                pool.terminate()
                sys.exit(1)
        else:
            t0 = time.time()
            unaries = [self.llgrad_unary(i, **kwargs) for i in range(self.n_blocks)]
            t1 = time.time()
            #print self.n_blocks, "unaries in", t1-t0, "seconds"
            pairs = [self.llgrad_joint(i, j, **kwargs) for (i,j) in neighbors]
            t2 = time.time()
            #print len(neighbors), "pairs in", t2-t1, "seconds"

        t0 = time.time()

        unary_lls, unary_gradX, unary_gradCov = zip(*unaries)
        if len(pairs) > 0:
            pair_lls, pair_gradX, pair_gradCov = zip(*pairs)
        else:
            pair_lls = []
            pair_gradX = []
            pair_gradCov = []

        ll = np.sum(pair_lls)
        ll -= np.sum([(neighbor_count[i]-1)*ull for (i, ull) in enumerate(unary_lls) ])



        if "grad_X" in kwargs and kwargs['grad_X']:
            gradX = np.zeros(self.X.shape)
            pair_idx = 0
            for i in range(self.n_blocks):
                #i_start, i_end = self.block_boundaries[i]
                idxs = self.block_idxs[i]
                gradX[idxs, :] -= (neighbor_count[i]-1)*unary_gradX[i]

            for pair_idx, (i,j) in enumerate(neighbors):
                idxs = self.block_idxs[i]
                jdxs = self.block_idxs[j]
                #i_start, i_end = self.block_boundaries[i]
                #j_start, j_end = self.block_boundaries[j]
                ni = len(idxs)
                gradX[idxs] += pair_gradX[pair_idx][:ni]
                gradX[jdxs] += pair_gradX[pair_idx][ni:]
        else:
            gradX = np.zeros((0, 0))

        if "grad_cov" in kwargs and kwargs['grad_cov']:
            ncov = 2 + self.X.shape[1]
            if self.nonstationary:
                gradCov = [(1-neighbor_count[i])*unary_gradCov[i] for i in range(self.n_blocks)]
                for pair_idx, (i,j) in enumerate(neighbors):
                    pgcov = pair_gradCov[pair_idx]
                    gradCov[i] += pgcov[:ncov]
                    gradCov[j] += pgcov[ncov:]
            else:
                gradCov = np.sum(pair_gradCov, axis=0)
                gradCov -= np.sum([(neighbor_count[i]-1)*unary_gradCov[i] for i in range(self.n_blocks)], axis=0)
                gradCov = gradCov.reshape((1, -1))

        else:
            gradCov = np.zeros((0, 0))

        t1 = time.time()


        return ll, gradX, gradCov

    def llgrad_blanket(self, i, **kwargs):
        # compute the terms of the objective corresponding to the Markov 
        # blanket of block i. Optimizing these terms with respect to X_i
        # is a coordinate ascent step on the full objective.
        idxs = self.block_idxs[i]
        n_block = len(idxs)

        # ignore gradCov return value since it's nonsensical to
        # optimize global covariance hparams over local blankets
        ll, gradX, _ = self.llgrad_unary(i, **kwargs)
        ll *= (1-self.neighbor_count[i])
        gradX *= (1-self.neighbor_count[i])

        for j in self.neighbor_dict[i]:
            pll, pgX, _ = self.llgrad_joint(i, j, **kwargs)
            ll += pll
            gradX += pgX[:n_block]
        return ll, gradX

    def llgrad_unary(self, i, **kwargs):
        idxs = self.block_idxs[i]
        X = self.X[idxs]
        #print "unary %d size %d" % (i, len(idxs))

        if self.nonstationary:
            kwargs['block_i'] = i

        if self.kernelized:
            YY = self.YY[idxs][:, idxs] #[i_start:i_end, i_start:i_end]
            return self.gaussian_llgrad_kernel(X, YY, dy=self.dy, **kwargs)
        else:
            Y = self.Y[idxs]
            return self.gaussian_llgrad(X, Y, **kwargs)

    def llgrad_joint(self, i, j, **kwargs):

        #i_start, i_end = self.block_boundaries[i]
        #j_start, j_end = self.block_boundaries[j]
        idxs = self.block_idxs[i]
        jdxs = self.block_idxs[j]
        #print "joint %d %d  size %d %d" % (i, j, len(idxs), len(jdxs))
        Xi = self.X[idxs]
        Xj = self.X[jdxs]

        ni = Xi.shape[0]
        nj = Xj.shape[0]
        X = np.vstack([Xi, Xj])

        if self.nonstationary:
            kwargs['block_i'] = i
            kwargs['block_j'] = j


        if self.kernelized:
            raise Exception("need to fix indices for kernelized joint llgrad")
            YY = np.empty((ni+nj, ni+nj))
            YY[:ni, :ni] = self.YY[i_start:i_end, i_start:i_end]
            YY[ni:, ni:] = self.YY[j_start:j_end, j_start:j_end]
            YY[:ni, ni:] = self.YY[i_start:i_end, j_start:j_end]
            YY[ni:, :ni]  = YY[:ni, ni:].T
            return self.gaussian_llgrad_kernel(X, YY, dy=self.dy, **kwargs)
        else:
            Yi = self.Y[idxs]
            Yj = self.Y[jdxs]
            Y = np.vstack([Yi, Yj])
            return self.gaussian_llgrad(X, Y, **kwargs)


    def kernel(self, X, X2=None, block=None):
        if block is None:
            ptree = self.predict_tree
            nv = self.noise_var
        else:
            ptree = self.block_trees[block]
            nv = self.block_covs[block][0]

        if X2 is None:
            n = X.shape[0]
            K = ptree.kernel_matrix(X, X, False)
            K += np.eye(n) * nv
        else:
            K = ptree.kernel_matrix(X, X2, False)
        return K

    def dKdx(self, X, p, i, return_vec=False, dKv=None, block=None):
        # derivative of kernel(X1, X2) wrt i'th coordinate of p'th point in X1.
        if block is None:
            ptree = self.predict_tree
        else:
            ptree = self.block_trees[block]

        if return_vec:
            if dKv is None:
                dKv = np.zeros((X.shape[0],), dtype=np.float)
            ptree.kernel_deriv_wrt_xi_row(X, p, i, dKv)
            dKv[p] = 0
            return dKv
        else:
            dK = ptree.kernel_deriv_wrt_xi(X, X, p, i)
            dK[p,p] = 0
            dK = dK + dK.T
            return dK

    def dKdi(self, X1, i, block=None):
        if block is None:
            ptree = self.predict_tree
            cov = self.cov
        else:
            ptree = self.block_trees[block]
            cov = self.block_covs[block][1]

        if (i == 0):
            dKdi = np.eye(X1.shape[0])
        elif (i == 1):
            if (len(cov.wfn_params) != 1):
                raise ValueError('gradient computation currently assumes just a single scaling parameter for weight function, but currently wfn_params=%s' % cov.wfn_params)
            dKdi = self.kernel(X1, X1, block=block) / cov.wfn_params[0]
        else:
            dc = ptree.kernel_matrix(X1, X1, True)
            dKdi = ptree.kernel_deriv_wrt_i(X1, X1, i-2, 1, dc)
        return dKdi

    
    def gaussian_llgrad_sparse(self, X, Y, grad_X = False, grad_cov=False, max_distance=5.0):
        import scipy.sparse
        import scipy.sparse.linalg
        import scikits.sparse.cholmod

        t0 = time.time()
        t0 = time.time()
        n, dx = X.shape

        dy = Y.shape[1]


        gradX = np.zeros(())
        gradC = np.zeros(())

        if n==0:
            if grad_X:
                gradX = np.zeros(X.shape)
            if grad_cov:
                ncov = 2 + len(self.cov.dfn_params)
                gradC = np.zeros((ncov,))
            return 0.0, gradX, gradC



        tree = VectorTree(X, 1, self.cov.dfn_str, self.cov.dfn_params, self.cov.wfn_str, self.cov.wfn_params)
        
        t05 = time.time()

        entries = tree.sparse_training_kernel_matrix(X, max_distance, False)
        #t06 = time.time()
        nzr, nzc, entries_K = np.array(entries[:,0], dtype=np.int32), np.array(entries[:,1], dtype=np.int32), entries[:,2]
        #t07 = time.time()
        distances = tree.sparse_distances(X, X, nzr, nzc);
        #t08 = time.time()
        spK = scipy.sparse.coo_matrix((entries_K, (nzr, nzc)), shape=(n,n), dtype=float)
        spK = spK + self.noise_var * scipy.sparse.eye(spK.shape[0])

        t1 = time.time()
        #print " sparse entires %.3f copy %.03f distances %.03f matrix %.03f" % (t06-t05, t07-t06, t08-t07, t1-t08)

        
        factor = scikits.sparse.cholmod.cholesky(spK)
        t11 = time.time()
        Alpha = factor(Y)
        t12 = time.time()
        prec = factor(scipy.sparse.eye(spK.shape[0]).tocsc())
        t13 = time.time()
        pprec = pdinv(spK.toarray())
        t133 = time.time()

        unpermuted_L = factor.L()
        logdet = np.sum(np.log(unpermuted_L.diagonal()))*2
        t14 = time.time()
        print " sparse factor %.3f alpha %.3f inv %.3f pinv %.3f logdet %.3f" % (t11-t1, t12-t11, t13-t12, t133-t13, t14-t133)


        ll = -.5 * np.sum(Y*Alpha)
        ll += -.5 * dy * logdet 
        ll += -.5 * dy * n * np.log(2*np.pi)

        t2 = time.time()
        if grad_X:
            gradX = np.zeros((n, dx))
    
            for i in range(dx):
                dK_entries = tree.sparse_kernel_deriv_wrt_xi(X, i, nzr, nzc, distances)
                sdK = scipy.sparse.coo_matrix((dK_entries, (nzr, nzc)), shape=spK.shape, dtype=float)
                d_logdet = -dy * np.asarray(sdK.multiply(prec).sum(axis=1)).reshape((-1,))

                dK_alpha = sdK * Alpha
                scaled = dK_alpha * Alpha
                gradX[:, i] = d_logdet + np.sum(scaled, axis=1)


        t3 = time.time()
        if grad_cov:
            ncov = 2 + len(self.cov.dfn_params)
            gradC = np.zeros((ncov,))
            for i in range(ncov):

                if (i == 0):
                    dKdi = scipy.sparse.eye(X.shape[0])
                elif (i == 1):
                    if (len(self.cov.wfn_params) != 1):
                        raise ValueError('gradient computation currently assumes just a single scaling parameter for weight function, but currently wfn_params=%s' % cov.wfn_params)
                    dKdi = (spK - (self.noise_var * scipy.sparse.eye(spK.shape[0]))) / self.cov.wfn_params[0]
                else:
                    dK_entries = tree.sparse_kernel_deriv_wrt_i(X, X,  nzr, nzc, i-2, distances)
                    #dKdi = self.dKdi(X, i, block=block_i)
                    dKdi = scipy.sparse.coo_matrix((dK_entries, (nzr, nzc)), shape=spK.shape, dtype=float)

                dlldi = .5 * np.sum(np.multiply(Alpha,dKdi* Alpha))
                dlldi -= .5 * dy * dKdi.multiply(prec).sum()
                gradC[i] = dlldi

        t4 = time.time()
        print "sparse tree %.4f kernel %.3f ll %.3f gradX %.3f gradC %.3f total %.3f" % (t05-t0, t1-t05, t2-t1, t3-t2, t4-t3, t4-t0)


    def gaussian_llgrad(self, X, Y, grad_X = False, grad_cov=False, block_i=None, block_j=None, block_split_n=None):

        t0 = time.time()
        n, dx = X.shape

        dy = Y.shape[1]


        gradX = np.zeros(())
        gradC = np.zeros(())

        if n==0:
            if grad_X:
                gradX = np.zeros(X.shape)
            if grad_cov:
                ncov = 2 + len(self.cov.dfn_params)
                gradC = np.zeros((ncov,))
            return 0.0, gradX, gradC

        K = self.kernel(X, block=block_i)
        t1 = time.time()

        #prec = np.linalg.inv(K)
        #Alpha = np.dot(prec, Y)
        prec, L, Lprec, logdet = pdinv(K)
        Alpha, _ = dpotrs(L, Y, lower=1)

        """
        numerical_error = check_inv(prec, K)

        if numerical_error > 1e-4:
            print "numerical failure in gaussian_llgrad, ks %.4f %.4f %.4f %.4f" % (k1, k2, k3, k4)
            ll = -1e10

            if grad_X:
                gradX = np.zeros((n, dx))
            if grad_cov:
                ncov = 2 + len(self.cov.dfn_params)
                gradC = -np.ones((ncov,))

            return ll, gradX, gradC
        """

        ll = -.5 * np.sum(Y*Alpha)
        ll += -.5 * dy * logdet #np.linalg.slogdet(K)[1]
        ll += -.5 * dy * n * np.log(2*np.pi)

        t2 = time.time()
        if grad_X:
            gradX = np.zeros((n, dx))
            dcv = np.zeros((X.shape[0]), dtype=np.float)
            dcv2 = np.zeros((X.shape[0]), dtype=np.float)
            dK_alpha = np.zeros((X.shape[0]), dtype=np.float)

            dK = [np.zeros(K.shape) for i in range(dx)]


            for p in range(n):
                for i in range(dx):
                    dll = 0
                    self.dKdx(X, p, i, return_vec=True, dKv=dcv, block=block_i)

                    dK[i][p, :] = dcv


            """
            for p in range(n):
                for i in range(dx):
                    dll = 0

                    dcv = dK[i][p,:]

                    #t1 = -np.outer(prec[p,:], dcv)
                    #t1[:, p] = -np.dot(prec, dcv)
                    #dll_dcov = .5*ny*np.trace(t1)
                    dll = -dy * np.dot(prec[p,:], dcv)

                    #dK_Alpha = np.outer(dcv, Alpha[p, :])
                    #dK_Alpha = dcv * Alpha[p, :]
                    #k0 = np.sum(Alpha * dK_Alpha)
                    #rowP = np.dot(dcv, Alpha)
                    #dK_Alpha[p, :] = np.dot(dcv, Alpha)
                    #dll_dcov = .5 * np.sum(Alpha * dK_Alpha)
                    new_rowp= np.dot(dcv.T, Alpha)
                    k1 = np.dot(new_rowp, Alpha[p, :])
                    old_rowp = dcv[p] * Alpha[p, :]
                    k2 = k1 + np.dot(Alpha[p, :], new_rowp-old_rowp)
                    dll_dcov = .5* k2

                    dll += dll_dcov

                    ""
                    for j in range(dy):
                        alpha = Alpha[:,j]
                        dK_alpha = dcv * alpha[p]
                        dK_alpha[p] = np.dot(dcv, alpha)
                        dll_dcov = .5*np.dot(alpha, dK_alpha)
                        dll += dll_dcov
                    ""
                    gradX[p,i] = dll
            """

            #gradX2 = np.zeros(gradX.shape)
            for i in range(dx):
                dKi = dK[i]
                d_logdet = -dy * np.sum(prec * dKi, axis=1)
                gradX[:, i] = d_logdet
                dK_alpha = np.dot(dKi, Alpha)
                scaled = dK_alpha * Alpha
                gradX[:, i] += np.sum(scaled, axis=1)

        t3 = time.time()

        if grad_cov:
            ncov = 2 + len(self.cov.dfn_params)
            gradC = np.zeros((ncov,))
            for i in range(ncov):
                dKdi = self.dKdi(X, i, block=block_i)
                dlldi = .5 * np.sum(np.multiply(Alpha,np.dot(dKdi, Alpha)))
                dlldi -= .5 * dy * np.sum(np.sum(np.multiply(prec, dKdi)))
                gradC[i] = dlldi

        t4 = time.time()
        print "dense kernel %.3f ll %.3f gradX %.3f gradC %.3f total %.3f" % (t1-t0, t2-t1, t3-t2, t4-t3, t4-t0)

        self.gaussian_llgrad_sparse(X, Y, grad_X = grad_X, grad_cov=grad_cov, max_distance=5.0)

        #print "llgrad %d pts %.4s" % (n, t1-t0)
        return ll, gradX, gradC

    def train_predictor(self, test_cov=None, Y=None):

        if Y is None:
            Y = self.Y

        if test_cov is None:
            test_cov = self.cov
            test_ptree = self.predict_tree
        else:
            dummy_X = np.array([[0.0,] * self.X.shape[1],], dtype=float)
            test_ptree = VectorTree(dummy_X, 1, test_cov.dfn_str, test_cov.dfn_params,
                                    test_cov.wfn_str, test_cov.wfn_params)

        block_Kinvs = []
        block_Alphas = []
        for i in range(self.n_blocks):
            #i_start, i_end = self.block_boundaries[i]
            idxs = self.block_idxs[i]
            X = self.X[idxs]
            blockY = Y[idxs]
            K = self.kernel(X, block = i if self.nonstationary else None)
            Kinv = np.linalg.inv(K)
            Alpha = np.dot(Kinv, blockY)
            block_Kinvs.append(Kinv)
            block_Alphas.append(Alpha)

        def predict(Xstar, test_noise_var=0.0, local=False):

            prior_cov = test_ptree.kernel_matrix(Xstar, Xstar, False)
            prior_cov += np.eye(prior_cov.shape[0])*test_noise_var
            prior_prec = np.linalg.inv(prior_cov)

            prior_mean = np.zeros((Xstar.shape[0], Y.shape[1]))

            test_block_idxs = self.block_fn(Xstar)

            """
            if local:
                nearest = np.argmin([np.min(pair_distances(Xstar, self.X[idxs])) for idxs in self.block_idxs])
                neighbors = [nearest,]
            else:
                neighbors = range(self.n_blocks)
            """
            source_blocks = set()
            for i, idxs in enumerate(test_block_idxs):
                if len(idxs) == 0: continue

                source_blocks.add(i)
                for j in self.neighbor_dict[i]:
                    source_blocks.add(j)

            for i in source_blocks:

                idxs = self.block_idxs[i]
                X = self.X[idxs]

                ptree = self.block_trees[i] if self.nonstationary else self.predict_tree
                nv = self.block_covs[i][0] if self.nonstationary else self.noise_var

                Kinv = block_Kinvs[i]
                Kstar = ptree.kernel_matrix(Xstar, X, False)
                Kss = ptree.kernel_matrix(Xstar, Xstar, False)
                if test_noise_var > 0:
                    Kss += np.eye(Kss.shape[0]) * nv

                mean = np.dot(Kstar, block_Alphas[i])
                cov = Kss - np.dot(Kstar, np.dot(Kinv, Kstar.T))
                prec = np.linalg.inv(cov)
                pp = np.linalg.inv(Kss)
                message_prec = prec - pp
                weighted_mean = np.dot(prec, mean)
                prior_mean += weighted_mean
                prior_prec += message_prec

            final_cov = np.linalg.inv(prior_prec)
            final_mean = np.dot(final_cov, prior_mean)

            return final_mean, final_cov

        return predict

    def gaussian_llgrad_kernel(self, X, YY, dy=None, grad_X=False, grad_cov=False):
        if self.nonstationary:
            raise Exception("nonstationary not supported for kernel observations. (or at all, really).")
        n, dx = X.shape
        if dy is None:
            dy = self.dy
        gradX = np.array(())
        gradC = np.array(())

        K = self.kernel(X)
        Kinv = np.linalg.inv(K)
        prec = Kinv

        """
        numerical_error = check_inv(prec, K)
        if numerical_error > 1e-4:
            print "numerical failure in gaussian_llgrad, ks %.4f %.4f %.4f %.4f" % (k1, k2, k3, k4)
            ll = -1e10

            if grad_X:
                gradX = np.zeros((n, dx))
            if grad_cov:
                ncov = 2 + self.X.shape[1]
                gradC = -np.ones((ncov,))

            return ll, gradX, gradC
        """

        KYYK = np.dot(np.dot(Kinv, YY), Kinv)

        ll =  -.5 * np.sum(Kinv * YY)
        ll += -.5 * dy * np.linalg.slogdet(K)[1]
        ll += -.5 * dy * n * np.log(2*np.pi)

        if grad_X:
            gradX = np.zeros((n, dx))
            for p in range(n):
                for i in range(dx):
                    #dcv_full = self.dKdx(X, p, i)
                    #dll = -.5*np.sum(KYYK * dcv_full)
                    dcv = self.dKdx(X, p, i, return_vec=True)
                    dll = np.dot(KYYK[p,:], dcv)

                    dll += -dy * np.dot(prec[p,:], dcv)

                    gradX[p,i] = dll

                #t1 = -np.outer(prec[p,:], dcov_v)
                #t1[:, p] = -np.dot(prec, dcov_v)

        if grad_cov:
            ncov = 2 + len(self.cov.dfn_params)
            gradC = np.zeros((ncov,))
            for i in range(ncov):
                dKdi = self.dKdi(X, i)
                dll = .5*np.sum(KYYK * dKdi)
                dll += -.5 * dy * np.sum(prec * dKdi)
                gradC[i] = dll

                if np.isnan(dll):
                    import pdb; pdb.set_trace()

        return ll, gradX, gradC

    def __getstate__(self):
        d = self.__dict__.copy()
        del d['predict_tree']
        return d

    def __setstate__(self, d):
        self.__dict__ = d
        dummy_X = np.array([[0.0,] * self.X.shape[1],], dtype=float)
        self.predict_tree = VectorTree(dummy_X, 1, self.cov.dfn_str, self.cov.dfn_params, self.cov.wfn_str, self.cov.wfn_params)

from multiprocessing import Pool
import time

def llgrad_unary_shim(arg):
    return GPRF.llgrad_unary(*arg[1:], **arg[0])

def llgrad_joint_shim(arg):
    return GPRF.llgrad_joint(*arg[1:], **arg[0])
