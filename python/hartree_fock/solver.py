import numpy as np 
from scipy.optimize import root, brentq
from triqs.gf import *
import triqs.utility.mpi as mpi
from h5.formats import register_class

class LatticeSolver(object):

    """ Hartree-Fock Lattice solver for local interactions.

    Parameters
    ----------

    h0_k : TRIQS BlockGF on a Brillouin zone mesh
        Single-particle dispersion.

    h_int : TRIQS Operator instance
        Local interaction Hamiltonian

    gf_struct : list of pairs [ (str,int), ...]
        Structure of the Green's functions. It must be a
        list of pairs, each containing the name of the
        Green's function block as a string and the size of that block.
        For example: ``[ ('up', 3), ('down', 3) ]``.

    beta : float
        inverse temperature

    symmeties : optional, list of functions
        symmetry functions acting on self energy at each consistent step

    force_real : optional, bool
        True if the self energy is forced to be real

    """

    def __init__(self, h0_k, h_int, gf_struct, beta, symmetries=[], force_real=False):

        self.h0_k = h0_k.copy()
        self.h0_k_MF = h0_k.copy()
        self.n_k = len(self.h0_k.mesh)

        self.h_int = h_int
        self.gf_struct = gf_struct
        self.beta = beta
        self.symmetries = symmetries
        self.force_real = force_real

        if self.force_real:
            self.Sigma_HF = {bl: np.zeros((bl_size, bl_size)) for bl, bl_size in gf_struct}
        else:
            self.Sigma_HF = {bl: np.zeros((bl_size, bl_size), dtype=complex) for bl, bl_size in gf_struct}
        self.rho = {bl: np.zeros((bl_size, bl_size)) for bl, bl_size in gf_struct}

    def solve(self, N_target=None, mu=None, with_fock=True, one_shot=False, tol=None):

        """ Solve for the Hartree Fock self energy using a root finder method.
        The self energy is stored in the ``Sigma_HF`` object of the LatticeSolver instance.

        Parameters
        ----------

        N_target : optional, float
            target density per site. Can only be provided if mu is not provided

        mu: optional, float
            chemical potential. Can only be provided if N_target is not provided. Default is 0 if N_target is not provided

        with_fock : optional, bool
            True if the fock terms are included in the self energy. Default is True

        one_shot : optional, bool
            True if the calcualtion is just one shot and not self consistent. Default is False

        tol: optional, float
            Convergence tolerance to pass to root finder

        """
        # if mu is None and N_target is None:
        #     raise ValueError('Either mu or N_target must be provided')
        mpi.report(logo())
        if mu is not None and N_target is not None:
            raise ValueError('Only provide either mu or N_target, not both')
        
        if not N_target is None:
            self.fixed = 'density'
            self.N_target = N_target
        else:
            self.fixed = 'mu'
            if not mu is None:
                self.mu = mu
            else:
                self.mu = 0

        if self.fixed == 'density':
            mpi.report('Running Lattice Solver at fixed density of %.4f' %self.N_target)
        else:
            mpi.report('Running Lattice Solver at fixed chemical potential of %.4f' %self.mu)
        mpi.report('beta = %.4f' %self.beta)
        mpi.report('h_int =', self.h_int)
        if one_shot:
            mpi.report('mode: one shot')
        else:
            mpi.report('mode: self-consistent')
        mpi.report('Including Fock terms:', with_fock)

        #function to pass to root finder
        def target_function(Sigma_HF_flat):
            self.update_mean_field_dispersion(unflatten(Sigma_HF_flat, self.gf_struct, real=self.force_real))
            if self.fixed == 'density':
                self.update_mu(self.N_target)
            rho = self.update_rho()
            if self.force_real:
                Sigma_HF = {bl: np.zeros((bl_size, bl_size)) for bl, bl_size in self.gf_struct}
            else:
                Sigma_HF = {bl: np.zeros((bl_size, bl_size), dtype=complex) for bl, bl_size in self.gf_struct}
            for term, coef in self.h_int:
                bl1, u1 = term[0][1]
                bl2, u2 = term[3][1]
                bl3, u3 = term[1][1]
                bl4, u4 = term[2][1]

                assert(bl1 == bl2 and bl3 == bl4)
                Sigma_HF[bl1][u2, u1] += coef * rho[bl3][u4, u3]
                Sigma_HF[bl3][u4, u3] += coef * rho[bl1][u2, u1]

                if bl1 == bl3 and with_fock:
                    Sigma_HF[bl1][u4, u1] -= coef * rho[bl3][u2, u3]
                    Sigma_HF[bl3][u2, u3] -= coef * rho[bl1][u4, u1]
            for function in self.symmetries:
                Sigma_HF = function(Sigma_HF)
            if one_shot:
                return Sigma_HF
            return Sigma_HF_flat - flatten(Sigma_HF, real=self.force_real)

        Sigma_HF_init = self.Sigma_HF

        if one_shot:
            self.Sigma_HF = target_function(Sigma_HF_init)
        
        else: #self consistent Hartree-Fock
            if tol is None:
                root_finder = root(target_function, flatten(Sigma_HF_init), method='broyden1')
            else:
                root_finder = root(target_function, flatten(Sigma_HF_init), method='broyden1', tol=tol)
            if root_finder['success']:
                mpi.report('Self Consistent Hartree-Fock converged successfully')
                self.Sigma_HF = unflatten(root_finder['x'], self.gf_struct, self.force_real)
                with np.printoptions(suppress=True, precision=3):
                    for name, bl in self.Sigma_HF.items():
                        mpi.report('Sigma_HF[\'%s\']:'%name)
                        mpi.report(bl)
                    mpi.report('mu = %.4f' %self.mu)

            else:
                mpi.report('Hartree-Fock solver did not converge successfully.')
                mpi.report(root_finder['message'])

    def update_mean_field_dispersion(self, Sigma_HF):
        for bl, size in self.gf_struct:
            self.h0_k_MF[bl].data[:] = self.h0_k[bl].data + Sigma_HF[bl][None, ...]

    def update_rho(self):

        for bl, size in self.gf_struct:
            e, V = np.linalg.eigh(self.h0_k_MF[bl].data)
            e -= self.mu

            # density matrix = Sum fermi_function*|psi><psi|
            self.rho[bl] = np.einsum('kab,kb,kcb->ac', V, fermi(e, self.beta), V.conj())/self.n_k
        
        return self.rho

    
    def update_mu(self, N_target):

        energies = {}
        e_min = np.inf
        e_max = -np.inf
        for bl, size in self.gf_struct:
            energies[bl] = np.linalg.eigvalsh(self.h0_k_MF[bl].data)
            bl_min = energies[bl].min()
            bl_max = energies[bl].max()
            if bl_min < e_min:
                e_min = bl_min
            if bl_max > e_max:
                e_max = bl_max

        def target_function(mu):
            n = 0
            for bl, size in self.gf_struct:
                n += np.sum(fermi(energies[bl] - mu, self.beta)) / self.n_k
            return n - N_target
        mu = brentq(target_function, e_min, e_max)
        self.mu = mu
        return mu             

    def __reduce_to_dict__(self):
        store_dict = {'h0_k': self.h0_k, 'h0_k_MF': self.h0_k_MF, 'n_k': self.n_k,
                      'h_int': self.h_int, 'gf_struct': self.gf_struct, 'beta': self.beta,
                      'symmetries': self.symmetries, 'force_real': self.force_real,
                      'Sigma_HF': self.Sigma_HF, 'rho': self.rho, }
        if hasattr(self, 'mu'):
            store_dict['mu'] = self.mu

        return store_dict

    @classmethod
    def __factory_from_dict__(cls,name,D) :

        instance = cls(D['h0_k'], D['h_int'], D['gf_struct'], D['beta'],
                       D['symmetries'], D['force_real'])

        instance.h0_k_MF = D['h0_k_MF']
        instance.n_k = D['n_k']
        instance.Sigma_HF = D['Sigma_HF']
        instance.rho = D['rho']
        if 'mu' in D:
            instance.mu = D['mu']

        return instance


class ImpuritySolver(object):
    
    """ Hartree-Fock Impurity solver.

    Parameters
    ----------

    h_int : TRIQS Operator instance
        Local interaction Hamiltonian

    gf_struct : list of pairs [ (str,int), ...]
        Structure of the Green's functions. It must be a
        list of pairs, each containing the name of the
        Green's function block as a string and the size of that block.
        For example: ``[ ('up', 3), ('down', 3) ]``.

    beta : float
        inverse temperature

    n_iw: integer, optional.
        Number of matsubara frequencies in the Matsubara Green's function. Default is 1025. 

    symmeties : optional, list of functions
        symmetry functions acting on self energy at each consistent step

    """
    def __init__(self, h_int, gf_struct, beta, n_iw=1025, symmetries=[]):

        self.h_int = h_int
        self.gf_struct = gf_struct
        self.beta = beta
        self.n_iw = n_iw
        self.symmetries = symmetries

        self.Sigma_HF = {bl: np.zeros((bl_size, bl_size), dtype=complex) for bl, bl_size in gf_struct}

        name_list = []
        block_list = []
        for bl_name, bl_size in self.gf_struct:
            name_list.append(bl_name)
            block_list.append(GfImFreq(beta=beta, n_points=n_iw, target_shape=[bl_size, bl_size]))
        self.G0_iw = BlockGf(name_list=name_list, block_list=block_list)
        self.G_iw = self.G0_iw.copy()

    def solve(self, with_fock=True, one_shot=False):

        """ Solve for the Hartree Fock self energy using a root finder method.
        The self energy is stored in the ``Sigma_HF`` object of the ImpuritySolver instance.
        The Green's function is stored in the ``G_iw`` object of the ImpuritySolver instance. 

        Parameters
        ----------
        with_fock : optional, bool
            True if the fock terms are included in the self energy. Default is True

        one_shot : optional, bool
            True if the calcualtion is just one shot and not self consistent. Default is False

        """

        mpi.report(logo())
        mpi.report('Running Impurity Solver')
        mpi.report('beta = %.4f' %self.beta)
        mpi.report('h_int =', self.h_int)
        if one_shot:
            mpi.report('mode: one shot')
        else:
            mpi.report('mode: self-consistent')
        mpi.report('Including Fock terms:', with_fock)

        def f(Sigma_HF_flat):

            Sigma_HF = {bl: np.zeros((bl_size, bl_size), dtype=complex) for bl, bl_size in self.gf_struct}
            G_iw = self.G0_iw.copy()
            G_dens = {}
            Sigma_unflattened = unflatten(Sigma_HF_flat, self.gf_struct)
            for bl, G0_bl in self.G0_iw:
                G_iw[bl] << inverse(inverse(G0_bl) - Sigma_unflattened[bl])
                G_dens[bl] = G_iw[bl].density().real
        
            for term, coef in self.h_int:
                bl1, u1 = term[0][1]
                bl2, u2 = term[3][1]
                bl3, u3 = term[1][1]
                bl4, u4 = term[2][1]

                assert(bl1 == bl2 and bl3 == bl4)

                Sigma_HF[bl1][u2, u1] += coef * G_dens[bl3][u4, u3]
                Sigma_HF[bl3][u4, u3] += coef * G_dens[bl1][u2, u1]

                if bl1 == bl3 and with_fock:
                    Sigma_HF[bl1][u4, u1] -= coef * G_dens[bl3][u2, u3]
                    Sigma_HF[bl3][u2, u3] -= coef * G_dens[bl1][u4, u1]
        
            for function in self.symmetries:
                Sigma_HF = function(Sigma_HF)

            if one_shot:
                return Sigma_HF
            return Sigma_HF_flat - flatten(Sigma_HF)

        Sigma_HF_init = self.Sigma_HF

        if one_shot:
            self.Sigma_HF = f(Sigma_HF_init)
            for bl, G0_bl in self.G0_iw:
                self.G_iw[bl] << inverse(inverse(G0_bl) - self.Sigma_HF[bl])

        
        else: #self consistent Hartree-Fock
            root_finder = root(f, flatten(Sigma_HF_init), method='broyden1')
            if root_finder['success']:
                mpi.report('Self Consistent Hartree-Fock converged successfully')
                self.Sigma_HF = unflatten(root_finder['x'], self.gf_struct)
                mpi.report('Calculated self energy:')
                with np.printoptions(suppress=True, precision=3):
                    for name, bl in self.Sigma_HF.items():
                        mpi.report('Sigma_HF[\'%s\']:'%name)
                        mpi.report(bl)
                for bl, G0_bl in self.G0_iw:
                    self.G_iw[bl] << inverse(inverse(G0_bl) - self.Sigma_HF[bl])

            else:
                mpi.report('Hartree-Fock solver did not converge successfully.')
                mpi.report(root_finder['message'])


    def interaction_energy(self):

        """ Calculate the interaction energy

        """
        E = 0        
        for bl, gbl in self.G_iw:
            E += 0.5 * np.trace(self.Sigma_HF[bl].dot(gbl.density()))
        return E

    def __reduce_to_dict__(self):
        store_dict = {'n_iw': self.n_iw, 'G0_iw': self.G0_iw, 'G_iw': self.G_iw,
                      'h_int': self.h_int, 'gf_struct': self.gf_struct, 'beta': self.beta,
                      'symmetries': self.symmetries, 'Sigma_HF': self.Sigma_HF}
        return store_dict

    @classmethod
    def __factory_from_dict__(cls,name,D) :

        instance = cls(D['h_int'], D['gf_struct'], D['beta'], D['n_iw'], D['symmetries'])
        instance.Sigma_HF = D['Sigma_HF']
        instance.G0_iw = D['G0_iw']
        instance.G_iw = D['G_iw']
        return instance


def flatten(Sigma_HF, real=False):
    """
    Flatten a dictionary of 2D Numpy arrays into a 1D Numpy array.

    Parameters
    ----------
    Sigma_HF : dictionary of 2D arrays
    
    real : optional, bool
        True if the Numpy arrays have a real dtype. Default is False.


    """
    if real:
        return np.array([Sig_bl.flatten() for bl, Sig_bl in Sigma_HF.items()]).flatten()
    else:
        return np.array([Sig_bl.flatten().view(float) for bl, Sig_bl in Sigma_HF.items()]).flatten()

def unflatten(Sigma_HF_flat, gf_struct, real=False):
    """
    Unflatten a 1D Numpy array back to a dictionary of 2D Numpy arrays, based on the structure in gf_struct.

    Parameters
    ----------
    Sigma_HF_flat : 1D Numpy array

    gf_struct: list of pairs [ (str,int), ...]
        Structure of the Green's functions. Used to
        construct the dictionary of 2D arrays. 
    
    real : optional, bool
        True if the Numpy array has a real dtype. Default is False.
    """

    offset = 0
    Sigma_HF = {}
    for bl, bl_size in gf_struct:
        if real:
            Sigma_HF[bl] =  Sigma_HF_flat[list(range(offset, offset + bl_size**2))].reshape((bl_size, bl_size))
            offset = offset + bl_size**2
        else:
            Sigma_HF[bl] =  Sigma_HF_flat[list(range(offset, offset + 2*bl_size**2))].view(complex).reshape((bl_size, bl_size))
            offset = offset + 2*bl_size**2
    return Sigma_HF
    

def fermi(e, beta):
    """
    Numerically stable version of the Fermi function

    Parameters
    ----------
    e : float or ndarray
        Energy minus chemical potential

    beta: float
        Inverse temperature 

    """
    return np.exp(-beta * e *(e>0))/(1 + np.exp(-beta*np.abs(e)))

def logo():
    logo = """
╔╦╗╦═╗╦╔═╗ ╔═╗  ┬ ┬┌─┐
 ║ ╠╦╝║║═╬╗╚═╗  ├─┤├┤ 
 ╩ ╩╚═╩╚═╝╚╚═╝  ┴ ┴└  
TRIQS: Hartree-Fock solver
"""
    return logo

register_class(LatticeSolver)
register_class(ImpuritySolver)
