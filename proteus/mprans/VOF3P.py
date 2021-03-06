# A type of -*- python -*- file
"""
An optimized volume-of-fluid  transport module
"""
from __future__ import absolute_import
from __future__ import division
from builtins import range
from past.utils import old_div
import numpy
from math import fabs
import proteus
from proteus import cfemIntegrals, Quadrature, Norms, Comm
from proteus.NonlinearSolvers import NonlinearEquation
from proteus.FemTools import (DOFBoundaryConditions,
                              FluxBoundaryConditions,
                              C0_AffineLinearOnSimplexWithNodalBasis)
from proteus.flcbdfWrappers import globalMax
from proteus.Profiling import memory
from proteus.Profiling import logEvent
from proteus.Transport import OneLevelTransport
from proteus.TransportCoefficients import TC_base
from proteus.SubgridError import SGE_base
from proteus.ShockCapturing import ShockCapturing_base
from . import cVOF3P


class SubgridError(SGE_base):

    def __init__(self, coefficients, nd):
        proteus.SubgridError.SGE_base.__init__(
            self, coefficients, nd, lag=False)

    def initializeElementQuadrature(self, mesh, t, cq):
        pass

    def updateSubgridErrorHistory(self, initializationPhase=False):
        pass

    def calculateSubgridError(self, q):
        pass


class ShockCapturing(ShockCapturing_base):

    def __init__(
            self,
            coefficients,
            nd,
            shockCapturingFactor=0.25,
            lag=True,
            nStepsToDelay=None):
        proteus.ShockCapturing.ShockCapturing_base.__init__(
            self, coefficients, nd, shockCapturingFactor, lag)
        self.nStepsToDelay = nStepsToDelay
        self.nSteps = 0
        if self.lag:
            logEvent("VOF3P.ShockCapturing: lagging requested but must lag the first step; switching lagging off and delaying")
            self.nStepsToDelay = 1
            self.lag = False

    def initializeElementQuadrature(self, mesh, t, cq):
        self.mesh = mesh
        self.numDiff = []
        self.numDiff_last = []
        for ci in range(self.nc):
            self.numDiff.append(cq[('numDiff', ci, ci)])
            self.numDiff_last.append(cq[('numDiff', ci, ci)])

    def updateShockCapturingHistory(self):
        self.nSteps += 1
        if self.lag:
            for ci in range(self.nc):
                self.numDiff_last[ci][:] = self.numDiff[ci]
        if self.lag == False and self.nStepsToDelay is not None and self.nSteps > self.nStepsToDelay:
            logEvent("VOF3P.ShockCapturing: switched to lagged shock capturing")
            self.lag = True
            self.numDiff_last = []
            for ci in range(self.nc):
                self.numDiff_last.append(self.numDiff[ci].copy())
        logEvent("VOF3P: max numDiff %e" % (globalMax(self.numDiff_last[0].max()),))


class NumericalFlux(
        proteus.NumericalFlux.Advection_DiagonalUpwind_Diffusion_IIPG_exterior):

    def __init__(self, vt, getPointwiseBoundaryConditions,
                 getAdvectiveFluxBoundaryConditions,
                 getDiffusiveFluxBoundaryConditions):
        proteus.NumericalFlux.Advection_DiagonalUpwind_Diffusion_IIPG_exterior.__init__(
            self,
            vt,
            getPointwiseBoundaryConditions,
            getAdvectiveFluxBoundaryConditions,
            getDiffusiveFluxBoundaryConditions)

class RKEV(proteus.TimeIntegration.SSP):
    from proteus import TimeIntegration
    """
    Wrapper for SSPRK time integration using EV

    ... more to come ...
    """

    def __init__(self, transport, timeOrder=1, runCFL=0.1, integrateInterpolationPoints=False):
        proteus.TimeIntegration.SSP.__init__(self, transport, integrateInterpolationPoints=integrateInterpolationPoints)
        self.runCFL = runCFL
        self.dtLast = None
        self.isAdaptive = True
        self.cfl = transport.q[('cfl', 0)]
        # Stuff particular for SSP
        self.timeOrder = timeOrder  # order of approximation
        self.nStages = timeOrder  # number of stages total
        self.lstage = 0  # last stage completed
        # storage vectors
        self.u_dof_last = {}
        self.m_old = {}
        # per component stage values, list with array at each stage
        for ci in range(self.nc):
            self.m_last[ci] = transport.q[('u',ci)].copy()
            self.m_old[ci] = transport.q[('u',ci)].copy()
            self.u_dof_last[ci] = transport.u[ci].dof.copy()

    # def set_dt(self, DTSET):
    #    self.dt = DTSET #  don't update t
    def choose_dt(self):
        maxCFL = 1.0e-6
        maxCFL = max(maxCFL, globalMax(self.cfl.max()))
        self.dt = old_div(self.runCFL, maxCFL)
        if self.dtLast is None:
            self.dtLast = self.dt
        self.t = self.tLast + self.dt
        self.substeps = [self.t for i in range(self.nStages)]  # Manuel is ignoring different time step levels for now

    def initialize_dt(self, t0, tOut, q):
        """
        Modify self.dt
        """
        self.tLast = t0
        self.choose_dt()
        self.t = t0 + self.dt

    def setCoefficients(self):
        """
        beta are all 1's here
        mwf not used right now
        """
        self.alpha = numpy.zeros((self.nStages, self.nStages), 'd')
        self.dcoefs = numpy.zeros((self.nStages), 'd')

    def updateStage(self):
        """
        Need to switch to use coefficients
        """
        # mwf debug
        #import pdb
        # pdb.set_trace()
        self.lstage += 1
        assert self.timeOrder in [1, 2, 3]
        assert self.lstage > 0 and self.lstage <= self.timeOrder
        if self.timeOrder == 3:
            if self.lstage == 1:
                logEvent("First stage of SSP33 method", level=4)
                for ci in range(self.nc):
                    # save stage at quad points
                    self.m_last[ci][:] = self.transport.q[('u',ci)]
                    # DOFs
                    self.transport.u_dof_old[:] = self.transport.u[ci].dof
            elif self.lstage == 2:
                logEvent("Second stage of SSP33 method", level=4)
                for ci in range(self.nc):
                    # Quad points
                    self.m_last[ci][:] = 1./4*self.transport.q[('u',ci)]
                    self.m_last[ci][:] += 3./4*self.m_old[ci]
                    # DOFs
                    self.transport.u_dof_old[:] = 1./4*self.transport.u[ci].dof
                    self.transport.u_dof_old[:] += 3./4* self.u_dof_last[ci]                
            elif self.lstage == 3:
                logEvent("Third stage of SSP33 method", level=4)
                for ci in range(self.nc):
                    # Quad points
                    self.m_last[ci][:] = 2./3*self.transport.q[('u',ci)]
                    self.m_last[ci][:] += 1./3*self.m_old[ci]
                    # DOFs
                    self.transport.u[0].dof[:] = 2./3*self.transport.u[ci].dof
                    self.transport.u[0].dof[:] += 1./3* self.u_dof_last[ci]
                    # update u_dof_old
                    self.transport.u_dof_old[:] = self.u_dof_last[ci]                    
        elif self.timeOrder == 2:
            if self.lstage == 1:
                logEvent("First stage of SSP22 method", level=4)
                for ci in range(self.nc):
                    # save stage at quad points
                    self.m_last[ci][:] = self.transport.q[('u',ci)]
                    # DOFs
                    self.transport.u_dof_old[:] = self.transport.u[ci].dof
            elif self.lstage == 2:
                logEvent("Second stage of SSP22 method", level=4)
                for ci in range(self.nc):
                    # Quad points
                    self.m_last[ci][:] = 1./2*self.transport.q[('u',ci)]
                    self.m_last[ci][:] += 1./2*self.m_old[ci]
                    # DOFs
                    self.transport.u[0].dof[:]  = 1./2*self.transport.u[ci].dof
                    self.transport.u[0].dof[:] += 1./2*self.u_dof_last[ci]
                    # update u_dof_old
                    self.transport.u_dof_old[:] = self.u_dof_last[ci]                    
        else:
            assert self.timeOrder == 1
            for ci in range(self.nc):
                self.m_last[ci][:] = self.transport.q[('u',ci)]

    def initializeTimeHistory(self, resetFromDOF=True):
        """
        Push necessary information into time history arrays
        """
        for ci in range(self.nc):
            self.m_old[ci][:] = self.transport.q[('u',ci)]
            self.m_last[ci][:] = self.transport.q[('u',ci)]
            self.u_dof_last[ci][:] = self.transport.u[ci].dof[:]

    def updateTimeHistory(self, resetFromDOF=False):
        """
        assumes successful step has been taken
        """
        self.t = self.tLast + self.dt
        for ci in range(self.nc):
            self.m_old[ci][:] = self.m_last[ci][:]
            self.u_dof_last[ci][:] = self.transport.u[ci].dof[:]
        self.lstage = 0
        self.dtLast = self.dt
        self.tLast = self.t

    def generateSubsteps(self, tList):
        """
        create list of substeps over time values given in tList. These correspond to stages
        """
        self.substeps = []
        tLast = self.tLast
        for t in tList:
            dttmp = t - tLast
            self.substeps.extend([tLast + dttmp for i in range(self.nStages)])
            tLast = t

    def resetOrder(self, order):
        """
        initialize data structures for stage updges
        """
        self.timeOrder = order  # order of approximation
        self.nStages = order  # number of stages total
        self.lstage = 0  # last stage completed
        self.substeps = [self.t for i in range(self.nStages)]

    def setFromOptions(self, nOptions):
        """
        allow classes to set various numerical parameters
        """
        if 'runCFL' in dir(nOptions):
            self.runCFL = nOptions.runCFL
        flags = ['timeOrder']
        for flag in flags:
            if flag in dir(nOptions):
                val = getattr(nOptions, flag)
                setattr(self, flag, val)
                if flag == 'timeOrder':
                    self.resetOrder(self.timeOrder)
                    
class Coefficients(proteus.TransportCoefficients.TC_base):
    from proteus.ctransportCoefficients import VOFCoefficientsEvaluate
    from proteus.ctransportCoefficients import VolumeAveragedVOFCoefficientsEvaluate
    from proteus.cfemIntegrals import copyExteriorElementBoundaryValuesFromElementBoundaryValues

    def __init__(
            self,
            LS_model=None,
            V_model=0,
            RD_model=None,
            ME_model=1,
            VOS_model=None,
            checkMass=True,
            epsFact=0.0,
            useMetrics=0.0,
            sc_uref=1.0,
            sc_beta=1.0,
            setParamsFunc=None,
            movingDomain=False,
            set_vos=None,
            EXPLICIT_METHOD=False,
            outputQuantDOFs=False):

        self.outputQuantDOFs=outputQuantDOFs
        self.EXPLICIT_METHOD=EXPLICIT_METHOD
        self.set_vos = set_vos
        self.useMetrics = useMetrics
        self.variableNames = ['vof']
        nc = 1
        mass = {0: {0: 'linear'}}
        advection = {0: {0: 'linear'}}
        hamiltonian = {}
        diffusion = {}
        potential = {}
        reaction = {}
        TC_base.__init__(self,
                         nc,
                         mass,
                         advection,
                         diffusion,
                         potential,
                         reaction,
                         hamiltonian,
                         self.variableNames,
                         movingDomain=movingDomain)
        self.epsFact = epsFact
        self.V_model = V_model
        self.modelIndex = ME_model
        self.RD_modelIndex = RD_model
        self.LS_modelIndex = LS_model
        self.VOS_model = VOS_model
        self.sc_uref = sc_uref
        self.sc_beta = sc_beta
        self.checkMass = checkMass
        # VRANS
        self.q_vos = None
        self.ebqe_vos = None
        self.setParamsFunc = setParamsFunc
        self.flowCoefficients = None
        self.movingDomain = movingDomain

    def initializeMesh(self, mesh):
        self.eps = self.epsFact * mesh.h

    def attachModels(self, modelList):
        # self
        self.model = modelList[self.modelIndex]

        # redistanced level set
        if self.RD_modelIndex is not None:
            self.rdModel = modelList[self.RD_modelIndex]
        # level set
        if self.LS_modelIndex is not None:
            self.lsModel = modelList[self.LS_modelIndex]
            self.q_phi = modelList[self.LS_modelIndex].q[('u', 0)]
            self.ebqe_phi = modelList[self.LS_modelIndex].ebqe[('u', 0)]
            if ('u', 0) in modelList[self.LS_modelIndex].ebq:
                self.ebq_phi = modelList[self.LS_modelIndex].ebq[('u', 0)]
        else:
            self.ebqe_phi = numpy.zeros(
                self.model.ebqe[
                    ('u', 0)].shape, 'd')  # cek hack, we don't need this
        # flow model
        # print "flow model
        # index------------",self.V_model,modelList[self.V_model].q.has_key(('velocity',0))
        if self.V_model is not None:
            if ('velocity', 0) in modelList[self.V_model].q:
                self.q_v = modelList[self.V_model].q[('velocity', 0)]
                self.ebqe_v = modelList[
                    self.V_model].ebqe[
                    ('velocity', 0)]
            else:
                self.q_v = modelList[self.V_model].q[('f', 0)]
                self.ebqe_v = modelList[self.V_model].ebqe[('f', 0)]
            if ('velocity', 0) in modelList[self.V_model].ebq:
                self.ebq_v = modelList[
                    self.V_model].ebq[
                    ('velocity', 0)]
            else:
                if ('f', 0) in modelList[self.V_model].ebq:
                    self.ebq_v = modelList[self.V_model].ebq[('f', 0)]
        self.flowCoefficients = modelList[self.V_model].coefficients
        if self.VOS_model is not None:
            self.model.q_vos = modelList[self.VOS_model].q[('u',0)].copy()
            self.model.ebqe_vos = modelList[self.VOS_model].ebqe[('u',0)].copy()
            self.q_vos = self.model.q_vos
            self.ebqe_vos = self.model.ebqe_vos
        else:
            self.q_porosity = numpy.ones(
                modelList[
                    self.modelIndex].q[
                    ('u', 0)].shape, 'd')
            if self.setParamsFunc is not None:
                self.setParamsFunc(
                    modelList[
                        self.modelIndex].q['x'],
                    self.q_porosity)
            self.q_vos = 1.0 - self.q_porosity
            if hasattr(self.flowCoefficients, 'ebq_porosity'):
                self.ebq_porosity = self.flowCoefficients.ebq_porosity
                self.ebq_vos = 1.0 - self.ebq_porosity
            elif ('u', 0) in modelList[self.modelIndex].ebq:
                self.ebq_porosity = numpy.ones(
                    modelList[
                        self.modelIndex].ebq[
                        ('u', 0)].shape, 'd')
                if self.setParamsFunc is not None:
                    self.setParamsFunc(
                        modelList[
                            self.modelIndex].ebq['x'],
                        self.ebq_porosity)
                self.ebq_vos = 1.0 - self.ebq_porosity
            if hasattr(self.flowCoefficients, 'ebqe_porosity'):
                self.ebqe_porosity = self.flowCoefficients.ebqe_porosity
                self.ebqe_vos = 1.0 - self.ebqe_porosity
            else:
                self.ebqe_porosity = numpy.ones(
                    modelList[
                        self.LS_modelIndex].ebqe[
                        ('u', 0)].shape, 'd')
                if self.setParamsFunc is not None:
                    self.setParamsFunc(
                        modelList[
                            self.LS_modelIndex].ebqe['x'],
                        self.ebqe_porosity)
                self.ebqe_vos = 1.0 - self.ebqe_porosity

    def initializeElementQuadrature(self, t, cq):
        if self.V_model is None:
            self.q_v = numpy.ones(cq[('f', 0)].shape, 'd')
        # VRANS
        self.q_porosity = numpy.ones(cq[('u', 0)].shape, 'd')

    def initializeElementBoundaryQuadrature(self, t, cebq, cebq_global):
        if self.V_model is None:
            self.ebq_v = numpy.ones(cebq[('f', 0)].shape, 'd')
        # VRANS
        self.ebq_porosity = numpy.ones(cebq[('u', 0)].shape, 'd')

    def initializeGlobalExteriorElementBoundaryQuadrature(self, t, cebqe):
        if self.V_model is None:
            self.ebqe_v = numpy.ones(cebqe[('f', 0)].shape, 'd')
        # VRANS
        self.ebqe_porosity = numpy.ones(cebqe[('u', 0)].shape, 'd')

    def preStep(self, t, firstStep=False):
        # SAVE OLD SOLUTION #
        self.model.u_dof_old[:] = self.model.u[0].dof

        # Restart flags for stages of taylor galerkin
        self.model.stage = 1
        self.model.auxTaylorGalerkinFlag = 1
        
        # COMPUTE NEW VELOCITY (if given by user) #
        if self.model.hasVelocityFieldAsFunction:
            self.model.updateVelocityFieldAsFunction()

        if self.checkMass:
            self.m_pre = Norms.scalarDomainIntegral(
                self.model.q['dV_last'], self.model.q[
                    ('m', 0)], self.model.mesh.nElements_owned)
            logEvent("Phase  0 mass before VOF3P step = %12.5e" %
                (self.m_pre,), level=2)
        #     self.m_last = Norms.scalarDomainIntegral(self.model.q['dV'],
        #                                              self.model.timeIntegration.m_last[0],
        #                                              self.model.mesh.nElements_owned)
        #     logEvent("Phase  0 mass before VOF3P (m_last) step = %12.5e" % (self.m_last,),level=2)
        copyInstructions = {}
        return copyInstructions

    def postStep(self, t, firstStep=False):
        #input("STOP")
        self.model.q['dV_last'][:] = self.model.q['dV']
        if self.checkMass:
            self.m_post = Norms.scalarDomainIntegral(
                self.model.q['dV'], self.model.q[
                    ('m', 0)], self.model.mesh.nElements_owned)
            logEvent("Phase  0 mass after VOF3P step = %12.5e" %
                (self.m_post,), level=2)
            # self.fluxIntegral = Norms.fluxDomainBoundaryIntegral(self.model.ebqe['dS'],
            #                                                     self.model.ebqe[('advectiveFlux',0)],
            #                                                     self.model.mesh)
            #logEvent("Phase  0 mass flux boundary integral after VOF step = %12.5e" % (self.fluxIntegral,),level=2)
            #logEvent("Phase  0 mass conservation after VOF step = %12.5e" % (self.m_post - self.m_last + self.model.timeIntegration.dt*self.fluxIntegral,),level=2)
            # divergence = Norms.fluxDomainBoundaryIntegralFromVector(self.model.ebqe['dS'],
            #                                                        self.ebqe_v,
            #                                                        self.model.ebqe['n'],
            #                                                        self.model.mesh)
            #logEvent("Divergence = %12.5e" % (divergence,),level=2)
        copyInstructions = {}
        return copyInstructions

    def updateToMovingDomain(self, t, c):
        # in a moving domain simulation the velocity coming in is already for
        # the moving domain
        pass

    def evaluate(self, t, c):
        # mwf debug
        # print "VOF3Pcoeficients eval t=%s " % t
        if c[('f', 0)].shape == self.q_v.shape:
            v = self.q_v
            phi = self.q_phi
            porosity = self.q_porosity
        elif c[('f', 0)].shape == self.ebqe_v.shape:
            v = self.ebqe_v
            phi = self.ebqe_phi
            porosity = self.ebq_porosity
        elif ((self.ebq_v is not None and self.ebq_phi is not None) and c[('f', 0)].shape == self.ebq_v.shape):
            v = self.ebq_v
            phi = self.ebq_phi
            porosity = self.ebq_porosity
        else:
            v = None
            phi = None
            porosity = None
        if v is not None:
            # self.VOF3PCoefficientsEvaluate(self.eps,
            #                              v,
            #                              phi,
            #                              c[('u',0)],
            #                              c[('m',0)],
            #                              c[('dm',0,0)],
            #                              c[('f',0)],
            #                              c[('df',0,0)])
            self.VolumeAveragedVOFCoefficientsEvaluate(self.eps,
                                                       v,
                                                       phi,
                                                       porosity,
                                                       c[('u', 0)],
                                                       c[('m', 0)],
                                                       c[('dm', 0, 0)],
                                                       c[('f', 0)],
                                                       c[('df', 0, 0)])
        # if self.checkMass:
        #     logEvent("Phase  0 mass in eavl = %12.5e" % (Norms.scalarDomainIntegral(self.model.q['dV'],
        #                                                                        self.model.q[('m',0)],
        # self.model.mesh.nElements_owned),),level=2)


class LevelModel(proteus.Transport.OneLevelTransport):
    nCalls = 0

    def __init__(self,
                 uDict,
                 phiDict,
                 testSpaceDict,
                 matType,
                 dofBoundaryConditionsDict,
                 dofBoundaryConditionsSetterDict,
                 coefficients,
                 elementQuadrature,
                 elementBoundaryQuadrature,
                 fluxBoundaryConditionsDict=None,
                 advectiveFluxBoundaryConditionsSetterDict=None,
                 diffusiveFluxBoundaryConditionsSetterDictDict=None,
                 stressTraceBoundaryConditionsSetterDict=None,
                 stabilization=None,
                 shockCapturing=None,
                 conservativeFluxDict=None,
                 numericalFluxType=None,
                 TimeIntegrationClass=None,
                 massLumping=False,
                 reactionLumping=False,
                 options=None,
                 name='defaultName',
                 reuse_trial_and_test_quadrature=True,
                 sd=True,
                 movingDomain=False,
                 bdyNullSpace=False):
        self.bdyNullSpace = bdyNullSpace
        #
        # set the objects describing the method and boundary conditions
        #
        self.movingDomain = movingDomain
        self.tLast_mesh = None
        #
        self.name = name
        self.sd = sd
        self.Hess = False
        self.lowmem = True
        self.timeTerm = True  # allow turning off  the  time derivative
        # self.lowmem=False
        self.testIsTrial = True
        self.phiTrialIsTrial = True
        self.u = uDict
        self.ua = {}  # analytical solutions
        self.phi = phiDict
        self.dphi = {}
        self.matType = matType
        # mwf try to reuse test and trial information across components if
        # spaces are the same
        self.reuse_test_trial_quadrature = reuse_trial_and_test_quadrature  # True#False
        if self.reuse_test_trial_quadrature:
            for ci in range(1, coefficients.nc):
                assert self.u[ci].femSpace.__class__.__name__ == self.u[
                    0].femSpace.__class__.__name__, "to reuse_test_trial_quad all femSpaces must be the same!"
        self.u_dof_old = None
        # Simplicial Mesh
        # assume the same mesh for  all components for now
        self.mesh = self.u[0].femSpace.mesh
        self.testSpace = testSpaceDict
        self.dirichletConditions = dofBoundaryConditionsDict
        # explicit Dirichlet  conditions for now, no Dirichlet BC constraints
        self.dirichletNodeSetList = None
        self.coefficients = coefficients
        self.coefficients.initializeMesh(self.mesh)
        self.nc = self.coefficients.nc
        self.stabilization = stabilization
        self.shockCapturing = shockCapturing
        # no velocity post-processing for now
        self.conservativeFlux = conservativeFluxDict
        self.fluxBoundaryConditions = fluxBoundaryConditionsDict
        self.advectiveFluxBoundaryConditionsSetterDict = advectiveFluxBoundaryConditionsSetterDict
        self.diffusiveFluxBoundaryConditionsSetterDictDict = diffusiveFluxBoundaryConditionsSetterDictDict
        # determine whether  the stabilization term is nonlinear
        self.stabilizationIsNonlinear = False
        # cek come back
        if self.stabilization is not None:
            for ci in range(self.nc):
                if ci in coefficients.mass:
                    for flag in list(coefficients.mass[ci].values()):
                        if flag == 'nonlinear':
                            self.stabilizationIsNonlinear = True
                if ci in coefficients.advection:
                    for flag in list(coefficients.advection[ci].values()):
                        if flag == 'nonlinear':
                            self.stabilizationIsNonlinear = True
                if ci in coefficients.diffusion:
                    for diffusionDict in list(coefficients.diffusion[ci].values()):
                        for flag in list(diffusionDict.values()):
                            if flag != 'constant':
                                self.stabilizationIsNonlinear = True
                if ci in coefficients.potential:
                    for flag in list(coefficients.potential[ci].values()):
                        if flag == 'nonlinear':
                            self.stabilizationIsNonlinear = True
                if ci in coefficients.reaction:
                    for flag in list(coefficients.reaction[ci].values()):
                        if flag == 'nonlinear':
                            self.stabilizationIsNonlinear = True
                if ci in coefficients.hamiltonian:
                    for flag in list(coefficients.hamiltonian[ci].values()):
                        if flag == 'nonlinear':
                            self.stabilizationIsNonlinear = True
        # determine if we need element boundary storage
        self.elementBoundaryIntegrals = {}
        for ci in range(self.nc):
            self.elementBoundaryIntegrals[ci] = (
                (self.conservativeFlux is not None) or (
                    numericalFluxType is not None) or (
                    self.fluxBoundaryConditions[ci] == 'outFlow') or (
                    self.fluxBoundaryConditions[ci] == 'mixedFlow') or (
                    self.fluxBoundaryConditions[ci] == 'setFlow'))
        #
        # calculate some dimensions
        #
        # assume same space dim for all variables
        self.nSpace_global = self.u[0].femSpace.nSpace_global
        self.nDOF_trial_element = [
            u_j.femSpace.max_nDOF_element for u_j in list(self.u.values())]
        self.nDOF_phi_trial_element = [
            phi_k.femSpace.max_nDOF_element for phi_k in list(self.phi.values())]
        self.n_phi_ip_element = [
            phi_k.femSpace.referenceFiniteElement.interpolationConditions.nQuadraturePoints for phi_k in list(self.phi.values())]
        self.nDOF_test_element = [
            femSpace.max_nDOF_element for femSpace in list(self.testSpace.values())]
        self.nFreeDOF_global = [
            dc.nFreeDOF_global for dc in list(self.dirichletConditions.values())]
        self.nVDOF_element = sum(self.nDOF_trial_element)
        self.nFreeVDOF_global = sum(self.nFreeDOF_global)
        #
        NonlinearEquation.__init__(self, self.nFreeVDOF_global)
        #
        # build the quadrature point dictionaries from the input (this
        # is just for convenience so that the input doesn't have to be
        # complete)
        #
        elementQuadratureDict = {}
        elemQuadIsDict = isinstance(elementQuadrature, dict)
        if elemQuadIsDict:  # set terms manually
            for I in self.coefficients.elementIntegralKeys:
                if I in elementQuadrature:
                    elementQuadratureDict[I] = elementQuadrature[I]
                else:
                    elementQuadratureDict[I] = elementQuadrature['default']
        else:
            for I in self.coefficients.elementIntegralKeys:
                elementQuadratureDict[I] = elementQuadrature
        if self.stabilization is not None:
            for I in self.coefficients.elementIntegralKeys:
                if elemQuadIsDict:
                    if I in elementQuadrature:
                        elementQuadratureDict[
                            ('stab',) + I[1:]] = elementQuadrature[I]
                    else:
                        elementQuadratureDict[
                            ('stab',) + I[1:]] = elementQuadrature['default']
                else:
                    elementQuadratureDict[
                        ('stab',) + I[1:]] = elementQuadrature
        if self.shockCapturing is not None:
            for ci in self.shockCapturing.components:
                if elemQuadIsDict:
                    if ('numDiff', ci, ci) in elementQuadrature:
                        elementQuadratureDict[('numDiff', ci, ci)] = elementQuadrature[
                            ('numDiff', ci, ci)]
                    else:
                        elementQuadratureDict[('numDiff', ci, ci)] = elementQuadrature[
                            'default']
                else:
                    elementQuadratureDict[
                        ('numDiff', ci, ci)] = elementQuadrature
        if massLumping:
            for ci in list(self.coefficients.mass.keys()):
                elementQuadratureDict[('m', ci)] = Quadrature.SimplexLobattoQuadrature(
                    self.nSpace_global, 1)
            for I in self.coefficients.elementIntegralKeys:
                elementQuadratureDict[
                    ('stab',) + I[1:]] = Quadrature.SimplexLobattoQuadrature(self.nSpace_global, 1)
        if reactionLumping:
            for ci in list(self.coefficients.mass.keys()):
                elementQuadratureDict[('r', ci)] = Quadrature.SimplexLobattoQuadrature(
                    self.nSpace_global, 1)
            for I in self.coefficients.elementIntegralKeys:
                elementQuadratureDict[
                    ('stab',) + I[1:]] = Quadrature.SimplexLobattoQuadrature(self.nSpace_global, 1)
        elementBoundaryQuadratureDict = {}
        if isinstance(elementBoundaryQuadrature, dict):  # set terms manually
            for I in self.coefficients.elementBoundaryIntegralKeys:
                if I in elementBoundaryQuadrature:
                    elementBoundaryQuadratureDict[
                        I] = elementBoundaryQuadrature[I]
                else:
                    elementBoundaryQuadratureDict[
                        I] = elementBoundaryQuadrature['default']
        else:
            for I in self.coefficients.elementBoundaryIntegralKeys:
                elementBoundaryQuadratureDict[I] = elementBoundaryQuadrature
        #
        # find the union of all element quadrature points and
        # build a quadrature rule for each integral that has a
        # weight at each point in the union
        # mwf include tag telling me which indices are which quadrature rule?
        (self.elementQuadraturePoints, self.elementQuadratureWeights,
         self.elementQuadratureRuleIndeces) = Quadrature.buildUnion(elementQuadratureDict)
        self.nQuadraturePoints_element = self.elementQuadraturePoints.shape[0]
        self.nQuadraturePoints_global = self.nQuadraturePoints_element * \
            self.mesh.nElements_global
        #
        # Repeat the same thing for the element boundary quadrature
        #
        (self.elementBoundaryQuadraturePoints, self.elementBoundaryQuadratureWeights,
         self.elementBoundaryQuadratureRuleIndeces) = Quadrature.buildUnion(elementBoundaryQuadratureDict)
        self.nElementBoundaryQuadraturePoints_elementBoundary = self.elementBoundaryQuadraturePoints.shape[
            0]
        self.nElementBoundaryQuadraturePoints_global = (
            self.mesh.nElements_global *
            self.mesh.nElementBoundaries_element *
            self.nElementBoundaryQuadraturePoints_elementBoundary)
#        if isinstance(self.u[0].femSpace,C0_AffineLinearOnSimplexWithNodalBasis):
#            print self.nQuadraturePoints_element
#            if self.nSpace_global == 3:
#                assert(self.nQuadraturePoints_element == 5)
#            elif self.nSpace_global == 2:
#                assert(self.nQuadraturePoints_element == 6)
#            elif self.nSpace_global == 1:
#                assert(self.nQuadraturePoints_element == 3)
#
#            print self.nElementBoundaryQuadraturePoints_elementBoundary
#            if self.nSpace_global == 3:
#                assert(self.nElementBoundaryQuadraturePoints_elementBoundary == 4)
#            elif self.nSpace_global == 2:
#                assert(self.nElementBoundaryQuadraturePoints_elementBoundary == 4)
#            elif self.nSpace_global == 1:
#                assert(self.nElementBoundaryQuadraturePoints_elementBoundary == 1)

        #
        # storage dictionaries
        self.scalars_element = set()
        #
        # simplified allocations for test==trial and also check if space is mixed or not
        #
        self.q = {}
        self.ebq = {}
        self.ebq_global = {}
        self.ebqe = {}
        self.phi_ip = {}
        # mesh
        self.q['x'] = numpy.zeros((self.mesh.nElements_global,self.nQuadraturePoints_element,3),'d')
        self.ebqe['x'] = numpy.zeros(
            (self.mesh.nExteriorElementBoundaries_global,
             self.nElementBoundaryQuadraturePoints_elementBoundary,
             3),
            'd')
        self.q[('u', 0)] = numpy.zeros(
            (self.mesh.nElements_global, self.nQuadraturePoints_element), 'd')
        self.q[('dV_u', 0)] = (old_div(1.0, self.mesh.nElements_global)) * \
            numpy.ones((self.mesh.nElements_global, self.nQuadraturePoints_element), 'd')
        self.q[
            ('grad(u)',
             0)] = numpy.zeros(
            (self.mesh.nElements_global,
             self.nQuadraturePoints_element,
             self.nSpace_global),
            'd')
        self.q[('m', 0)] = self.q[('u', 0)]
        self.q[('m_last', 0)] = numpy.zeros(
            (self.mesh.nElements_global, self.nQuadraturePoints_element), 'd')
        self.q[('mt', 0)] = numpy.zeros(
            (self.mesh.nElements_global, self.nQuadraturePoints_element), 'd')
        self.q['dV'] = numpy.zeros(
            (self.mesh.nElements_global, self.nQuadraturePoints_element), 'd')
        self.q['dV_last'] = -1000 * \
            numpy.ones((self.mesh.nElements_global, self.nQuadraturePoints_element), 'd')
        self.q[('m_tmp', 0)] = self.q[('u', 0)]
        self.q[('cfl', 0)] = numpy.zeros(
            (self.mesh.nElements_global, self.nQuadraturePoints_element), 'd')
        self.q[('numDiff', 0, 0)] = numpy.zeros(
            (self.mesh.nElements_global, self.nQuadraturePoints_element), 'd')
        self.ebqe[
            ('u',
             0)] = numpy.zeros(
            (self.mesh.nExteriorElementBoundaries_global,
             self.nElementBoundaryQuadraturePoints_elementBoundary),
            'd')
        self.ebqe[
            ('grad(u)',
             0)] = numpy.zeros(
            (self.mesh.nExteriorElementBoundaries_global,
             self.nElementBoundaryQuadraturePoints_elementBoundary,
             self.nSpace_global),
            'd')
        self.ebqe[
            ('advectiveFlux_bc_flag',
             0)] = numpy.zeros(
            (self.mesh.nExteriorElementBoundaries_global,
             self.nElementBoundaryQuadraturePoints_elementBoundary),
            'i')
        self.ebqe[
            ('advectiveFlux_bc',
             0)] = numpy.zeros(
            (self.mesh.nExteriorElementBoundaries_global,
             self.nElementBoundaryQuadraturePoints_elementBoundary),
            'd')
        self.ebqe[
            ('advectiveFlux',
             0)] = numpy.zeros(
            (self.mesh.nExteriorElementBoundaries_global,
             self.nElementBoundaryQuadraturePoints_elementBoundary),
            'd')
        
        self.points_elementBoundaryQuadrature = set()
        self.scalars_elementBoundaryQuadrature = set(
            [('u', ci) for ci in range(self.nc)])
        self.vectors_elementBoundaryQuadrature = set()
        self.tensors_elementBoundaryQuadrature = set()
        self.inflowBoundaryBC = {}
        self.inflowBoundaryBC_values = {}
        self.inflowFlux = {}
        for cj in range(self.nc):
            self.inflowBoundaryBC[cj] = numpy.zeros(
                (self.mesh.nExteriorElementBoundaries_global,), 'i')
            self.inflowBoundaryBC_values[cj] = numpy.zeros(
                (self.mesh.nExteriorElementBoundaries_global, self.nDOF_trial_element[cj]), 'd')
            self.inflowFlux[cj] = numpy.zeros(
                (self.mesh.nExteriorElementBoundaries_global,
                 self.nElementBoundaryQuadraturePoints_elementBoundary),
                'd')
        self.internalNodes = set(range(self.mesh.nNodes_global))
        # identify the internal nodes this is ought to be in mesh
        # \todo move this to mesh
        for ebNE in range(self.mesh.nExteriorElementBoundaries_global):
            ebN = self.mesh.exteriorElementBoundariesArray[ebNE]
            eN_global = self.mesh.elementBoundaryElementsArray[ebN, 0]
            ebN_element = self.mesh.elementBoundaryLocalElementBoundariesArray[
                ebN, 0]
            for i in range(self.mesh.nNodes_element):
                if i != ebN_element:
                    I = self.mesh.elementNodesArray[eN_global, i]
                    self.internalNodes -= set([I])
        self.nNodes_internal = len(self.internalNodes)
        self.internalNodesArray = numpy.zeros((self.nNodes_internal,), 'i')
        for nI, n in enumerate(self.internalNodes):
            self.internalNodesArray[nI] = n
        #
        del self.internalNodes
        self.internalNodes = None
        logEvent("Updating local to global mappings", 2)
        self.updateLocal2Global()
        logEvent("Building time integration object", 2)
        logEvent(memory("inflowBC, internalNodes,updateLocal2Global",
                   "OneLevelTransport"), level=4)
        # mwf for interpolating subgrid error for gradients etc
        if self.stabilization and self.stabilization.usesGradientStabilization:
            self.timeIntegration = TimeIntegrationClass(
                self, integrateInterpolationPoints=True)
        else:
            self.timeIntegration = TimeIntegrationClass(self)

        if options is not None:
            self.timeIntegration.setFromOptions(options)
        logEvent(memory("TimeIntegration", "OneLevelTransport"), level=4)
        logEvent("Calculating numerical quadrature formulas", 2)
        self.calculateQuadrature()
        self.setupFieldStrides()

        comm = Comm.get()
        self.comm = comm
        if comm.size() > 1:
            assert numericalFluxType is not None and numericalFluxType.useWeakDirichletConditions, "You must use a numerical flux to apply weak boundary conditions for parallel runs"

        logEvent(memory("stride+offset", "OneLevelTransport"), level=4)
        if numericalFluxType is not None:
            if options is None or options.periodicDirichletConditions is None:
                self.numericalFlux = numericalFluxType(
                    self,
                    dofBoundaryConditionsSetterDict,
                    advectiveFluxBoundaryConditionsSetterDict,
                    diffusiveFluxBoundaryConditionsSetterDictDict)
            else:
                self.numericalFlux = numericalFluxType(
                    self,
                    dofBoundaryConditionsSetterDict,
                    advectiveFluxBoundaryConditionsSetterDict,
                    diffusiveFluxBoundaryConditionsSetterDictDict,
                    options.periodicDirichletConditions)
        else:
            self.numericalFlux = None
        # set penalty terms
        # cek todo move into numerical flux initialization
        if 'penalty' in self.ebq_global:
            for ebN in range(self.mesh.nElementBoundaries_global):
                for k in range(
                        self.nElementBoundaryQuadraturePoints_elementBoundary):
                    self.ebq_global['penalty'][ebN, k] = old_div(self.numericalFlux.penalty_constant, (
                        self.mesh.elementBoundaryDiametersArray[ebN]**self.numericalFlux.penalty_power))
        # penalty term
        # cek move  to Numerical flux initialization
        if 'penalty' in self.ebqe:
            for ebNE in range(self.mesh.nExteriorElementBoundaries_global):
                ebN = self.mesh.exteriorElementBoundariesArray[ebNE]
                for k in range(
                        self.nElementBoundaryQuadraturePoints_elementBoundary):
                    self.ebqe['penalty'][ebNE, k] = old_div(self.numericalFlux.penalty_constant, \
                        self.mesh.elementBoundaryDiametersArray[ebN]**self.numericalFlux.penalty_power)
        logEvent(memory("numericalFlux", "OneLevelTransport"), level=4)
        self.elementEffectiveDiametersArray = self.mesh.elementInnerDiametersArray
        # use post processing tools to get conservative fluxes, None by default
        from proteus import PostProcessingTools
        self.velocityPostProcessor = PostProcessingTools.VelocityPostProcessingChooser(
            self)
        logEvent(memory("velocity postprocessor", "OneLevelTransport"), level=4)
        # helper for writing out data storage
        from proteus import Archiver
        self.elementQuadratureDictionaryWriter = Archiver.XdmfWriter()
        self.elementBoundaryQuadratureDictionaryWriter = Archiver.XdmfWriter()
        self.exteriorElementBoundaryQuadratureDictionaryWriter = Archiver.XdmfWriter()
        # TODO get rid of this
        for ci, fbcObject in list(self.fluxBoundaryConditionsObjectsDict.items()):
            self.ebqe[('advectiveFlux_bc_flag', ci)] = numpy.zeros(
                self.ebqe[('advectiveFlux_bc', ci)].shape, 'i')
            for t, g in list(fbcObject.advectiveFluxBoundaryConditionsDict.items()):
                if ci in self.coefficients.advection:
                    self.ebqe[
                        ('advectiveFlux_bc', ci)][
                        t[0], t[1]] = g(
                        self.ebqe[
                            ('x')][
                            t[0], t[1]], self.timeIntegration.t)
                    self.ebqe[('advectiveFlux_bc_flag', ci)][t[0], t[1]] = 1

        if hasattr(self.numericalFlux, 'setDirichletValues'):
            self.numericalFlux.setDirichletValues(self.ebqe)
        if not hasattr(self.numericalFlux, 'isDOFBoundary'):
            self.numericalFlux.isDOFBoundary = {
                0: numpy.zeros(self.ebqe[('u', 0)].shape, 'i')}
        if not hasattr(self.numericalFlux, 'ebqe'):
            self.numericalFlux.ebqe = {
                ('u', 0): numpy.zeros(self.ebqe[('u', 0)].shape, 'd')}
        # TODO how to handle redistancing calls for
        # calculateCoefficients,calculateElementResidual etc
        self.globalResidualDummy = None
        compKernelFlag = 0
        self.vof = cVOF3P.VOF3P(
            self.nSpace_global,
            self.nQuadraturePoints_element,
            self.u[0].femSpace.elementMaps.localFunctionSpace.dim,
            self.u[0].femSpace.referenceFiniteElement.localFunctionSpace.dim,
            self.testSpace[0].referenceFiniteElement.localFunctionSpace.dim,
            self.nElementBoundaryQuadraturePoints_elementBoundary,
            compKernelFlag)

        self.forceStrongConditions = False
        if self.forceStrongConditions:
            self.dirichletConditionsForceDOF = DOFBoundaryConditions(
                self.u[0].femSpace,
                dofBoundaryConditionsSetterDict[0],
                weakDirichletConditions=False)

        if self.movingDomain:
            self.MOVING_DOMAIN = 1.0
        else:
            self.MOVING_DOMAIN = 0.0
        if self.mesh.nodeVelocityArray is None:
            self.mesh.nodeVelocityArray = numpy.zeros(
                self.mesh.nodeArray.shape, 'd')
        # mwf these are getting called by redistancing classes,

        # mql. Allow the user to provide functions to define the velocity field
        self.hasVelocityFieldAsFunction = False
        if ('velocityFieldAsFunction') in dir(options):
            self.velocityFieldAsFunction = options.velocityFieldAsFunction
            self.hasVelocityFieldAsFunction = True

        # For Taylor Galerkin methods
        self.stage = 1
        self.auxTaylorGalerkinFlag = 1
        self.uTilde_dof = numpy.zeros(self.u[0].dof.shape,'d')
        if self.coefficients.EXPLICIT_METHOD==True:
            self.useTwoStageNewton = True
        
        # Some asserts for NCLS with Taylor Galerkin
        if self.coefficients.EXPLICIT_METHOD==True:
            assert isinstance(self.timeIntegration,proteus.TimeIntegration.BackwardEuler_cfl), "If EXPLICIT_METHOD=True, use BackwardEuler_cfl"
            assert options.levelNonlinearSolver == proteus.NonlinearSolvers.TwoStageNewton, "If EXPLICIT_METHOD=True, use levelNonlinearSolver=TwoStageNewton"
            
    def updateVelocityFieldAsFunction(self):
        X = {0: self.q[('x')][:, :, 0],
             1: self.q[('x')][:, :, 1],
             2: self.q[('x')][:, :, 2]}
        t = self.timeIntegration.t
        self.coefficients.q_v[..., 0] = self.velocityFieldAsFunction[0](X, t)
        self.coefficients.q_v[..., 1] = self.velocityFieldAsFunction[1](X, t)
        if (self.nSpace_global == 3):
            self.coefficients.q_v[..., 2] = self.velocityFieldAsFunction[2](X, t)

        # BOUNDARY
        ebqe_X = {0: self.ebqe['x'][:, :, 0],
                  1: self.ebqe['x'][:, :, 1],
                  2: self.ebqe['x'][:, :, 2]}
        self.coefficients.ebqe_v[..., 0] = self.velocityFieldAsFunction[0](ebqe_X, t)
        self.coefficients.ebqe_v[..., 1] = self.velocityFieldAsFunction[1](ebqe_X, t)
        if (self.nSpace_global == 3):
            self.coefficients.ebqe_v[..., 2] = self.velocityFieldAsFunction[2](ebqe_X, t)
            
    def calculateCoefficients(self):
        pass

    def calculateElementResidual(self):
        if self.globalResidualDummy is not None:
            self.getResidual(self.u[0].dof, self.globalResidualDummy)

    def getResidual(self, u, r):
        import pdb
        import copy
        """
        Calculate the element residuals and add in to the global residual
        """

        if self.u_dof_old is None:
            # Pass initial condition to u_dof_old
            self.u_dof_old = numpy.copy(self.u[0].dof)

        if self.coefficients.set_vos:
            self.coefficients.set_vos(self.q['x'], self.coefficients.q_vos)
        r.fill(0.0)
        # Load the unknowns into the finite element dof
        self.timeIntegration.calculateCoefs()
        # print "***************max/min(m_last)*********************",max(self.timeIntegration.m_last[0].flat[:]),min(self.timeIntegration.m_last[0].flat[:])
        # print
        # "***************max/min(m_last)*********************",max(-self.timeIntegration.dt*self.timeIntegration.beta_bdf[0].flat[:]),min(-self.timeIntegration.dt*self.timeIntegration.beta_bdf[0].flat[:]),
        self.timeIntegration.calculateU(u)
        self.setUnknowns(self.timeIntegration.u)
        # cek can put in logic to skip of BC's don't depend on t or u
        # Dirichlet boundary conditions
        # if hasattr(self.numericalFlux,'setDirichletValues'):
        if (self.stage!=2):
            self.numericalFlux.setDirichletValues(self.ebqe)            
        # flux boundary conditions
        for t, g in list(self.fluxBoundaryConditionsObjectsDict[
                0].advectiveFluxBoundaryConditionsDict.items()):
            self.ebqe[
                ('advectiveFlux_bc', 0)][
                t[0], t[1]] = g(
                self.ebqe[
                    ('x')][
                    t[0], t[1]], self.timeIntegration.t)
            self.ebqe[('advectiveFlux_bc_flag', 0)][t[0], t[1]] = 1

        if self.forceStrongConditions:
            for dofN, g in list(self.dirichletConditionsForceDOF.DOFBoundaryConditionsDict.items()):
                self.u[0].dof[dofN] = g(
                    self.dirichletConditionsForceDOF.DOFBoundaryPointDict[dofN],
                    self.timeIntegration.t)
        assert (self.coefficients.q_porosity == 1).all()

        if (self.stage==2 and self.auxTaylorGalerkinFlag==1):
            self.uTilde_dof[:] = self.u[0].dof
            self.auxTaylorGalerkinFlag=0

        self.vof.calculateResidual(  # element
            self.u[0].femSpace.elementMaps.psi,
            self.u[0].femSpace.elementMaps.grad_psi,
            self.mesh.nodeArray,
            self.mesh.nodeVelocityArray,
            self.MOVING_DOMAIN,
            self.mesh.elementNodesArray,
            self.elementQuadratureWeights[('u', 0)],
            self.u[0].femSpace.psi,
            self.u[0].femSpace.grad_psi,
            self.u[0].femSpace.psi,
            self.u[0].femSpace.grad_psi,
            # element boundary
            self.u[0].femSpace.elementMaps.psi_trace,
            self.u[0].femSpace.elementMaps.grad_psi_trace,
            self.elementBoundaryQuadratureWeights[('u', 0)],
            self.u[0].femSpace.psi_trace,
            self.u[0].femSpace.grad_psi_trace,
            self.u[0].femSpace.psi_trace,
            self.u[0].femSpace.grad_psi_trace,
            self.u[0].femSpace.elementMaps.boundaryNormals,
            self.u[0].femSpace.elementMaps.boundaryJacobians,
            # physics
            self.mesh.nElements_global,
            self.coefficients.useMetrics,
            self.timeIntegration.alpha_bdf,
            self.shockCapturing.lag,
            self.shockCapturing.shockCapturingFactor,
            self.coefficients.sc_uref,
            self.coefficients.sc_beta,
            # VRANS start
            self.coefficients.q_vos,
            # VRANS end
            self.u[0].femSpace.dofMap.l2g,
            self.mesh.elementDiametersArray,
            self.u[0].dof,
            self.u_dof_old,
            self.coefficients.q_v,
            self.timeIntegration.m_tmp[0],
            self.q[('u', 0)],
            self.timeIntegration.beta_bdf[0],
            self.q['dV'],
            self.q['dV_last'],
            self.q[('cfl', 0)],
            self.shockCapturing.numDiff[0],
            self.shockCapturing.numDiff_last[0],
            self.offset[0], self.stride[0],
            r,
            self.mesh.nExteriorElementBoundaries_global,
            self.mesh.exteriorElementBoundariesArray,
            self.mesh.elementBoundaryElementsArray,
            self.mesh.elementBoundaryLocalElementBoundariesArray,
            self.coefficients.ebqe_v,
            # VRANS start
            self.coefficients.ebqe_vos,
            # VRANS end
            self.numericalFlux.isDOFBoundary[0],
            self.numericalFlux.ebqe[('u', 0)],
            self.ebqe[('advectiveFlux_bc_flag', 0)],
            self.ebqe[('advectiveFlux_bc', 0)],
            self.coefficients.ebqe_phi, self.coefficients.epsFact,
            self.ebqe[('u', 0)],
            self.ebqe[('advectiveFlux', 0)],
            #EXPLICIT METHODS
            self.coefficients.EXPLICIT_METHOD,
            self.u[0].femSpace.order,
            self.stage,
            self.uTilde_dof,
            self.timeIntegration.dt)

        #if self.coefficients.EXPLICIT_METHOD:
        #    self.taylorGalerkinStage = 2
        
        if self.forceStrongConditions:
            for dofN, g in list(self.dirichletConditionsForceDOF.DOFBoundaryConditionsDict.items()):
                r[dofN] = 0
        if self.stabilization:
            self.stabilization.accumulateSubgridMassHistory(self.q)

        #cell_based_cflMax = globalMax(self.q[('cfl', 0)].max()) * self.timeIntegration.dt
        #logEvent("...   Current dt = " + str(self.timeIntegration.dt), level=4)
        #logEvent("...   Maximum Cell Based CFL = " + str(cell_based_cflMax), level=2)
            
        logEvent("Global residual", level=9, data=r)
        self.nonlinear_function_evaluations += 1
        if self.globalResidualDummy is None:
            self.globalResidualDummy = numpy.zeros(r.shape, 'd')

    def getJacobian(self, jacobian):
        cfemIntegrals.zeroJacobian_CSR(self.nNonzerosInJacobian,
                                       jacobian)

        self.vof.calculateJacobian(  # element
            self.u[0].femSpace.elementMaps.psi,
            self.u[0].femSpace.elementMaps.grad_psi,
            self.mesh.nodeArray,
            self.mesh.nodeVelocityArray,
            self.MOVING_DOMAIN,
            self.mesh.elementNodesArray,
            self.elementQuadratureWeights[('u', 0)],
            self.u[0].femSpace.psi,
            self.u[0].femSpace.grad_psi,
            self.u[0].femSpace.psi,
            self.u[0].femSpace.grad_psi,
            # element boundary
            self.u[0].femSpace.elementMaps.psi_trace,
            self.u[0].femSpace.elementMaps.grad_psi_trace,
            self.elementBoundaryQuadratureWeights[('u', 0)],
            self.u[0].femSpace.psi_trace,
            self.u[0].femSpace.grad_psi_trace,
            self.u[0].femSpace.psi_trace,
            self.u[0].femSpace.grad_psi_trace,
            self.u[0].femSpace.elementMaps.boundaryNormals,
            self.u[0].femSpace.elementMaps.boundaryJacobians,
            self.mesh.nElements_global,
            self.coefficients.useMetrics,
            self.timeIntegration.alpha_bdf,
            self.shockCapturing.lag,
            self.shockCapturing.shockCapturingFactor,
            # VRANS start
            self.coefficients.q_vos,
            # VRANS end
            self.u[0].femSpace.dofMap.l2g,
            self.mesh.elementDiametersArray,
            self.u[0].dof,
            self.coefficients.q_v,
            self.timeIntegration.beta_bdf[0],
            self.q[('cfl', 0)],
            self.shockCapturing.numDiff_last[0],
            self.csrRowIndeces[(0, 0)], self.csrColumnOffsets[(0, 0)],
            jacobian,
            self.mesh.nExteriorElementBoundaries_global,
            self.mesh.exteriorElementBoundariesArray,
            self.mesh.elementBoundaryElementsArray,
            self.mesh.elementBoundaryLocalElementBoundariesArray,
            self.coefficients.ebqe_v,
            # VRANS start
            self.coefficients.ebqe_vos,
            # VRANS end
            self.numericalFlux.isDOFBoundary[0],
            self.numericalFlux.ebqe[('u', 0)],
            self.ebqe[('advectiveFlux_bc_flag', 0)],
            self.ebqe[('advectiveFlux_bc', 0)],
            self.csrColumnOffsets_eb[(0, 0)],
            #EXPLICIT METHODS
            self.coefficients.EXPLICIT_METHOD)
        
        # Load the Dirichlet conditions directly into residual
        if self.forceStrongConditions:
            scaling = 1.0  # probably want to add some scaling to match non-dirichlet diagonals in linear system
            for dofN in list(self.dirichletConditionsForceDOF.DOFBoundaryConditionsDict.keys()):
                global_dofN = dofN
                for i in range(
                    self.rowptr[global_dofN],
                    self.rowptr[
                        global_dofN + 1]):
                    if (self.colind[i] == global_dofN):
                            # print "RBLES forcing residual cj = %s dofN= %s
                            # global_dofN= %s was self.nzval[i]= %s now =%s " %
                            # (cj,dofN,global_dofN,self.nzval[i],scaling)
                        self.nzval[i] = scaling
                    else:
                        self.nzval[i] = 0.0
                        # print "RBLES zeroing residual cj = %s dofN= %s
                        # global_dofN= %s " % (cj,dofN,global_dofN)
        logEvent("Jacobian ", level=10, data=jacobian)
        # mwf decide if this is reasonable for solver statistics
        self.nonlinear_function_jacobian_evaluations += 1
        return jacobian

    def calculateElementQuadrature(self):
        """
        Calculate the physical location and weights of the quadrature rules
        and the shape information at the quadrature points.

        This function should be called only when the mesh changes.
        """
        self.u[0].femSpace.elementMaps.getValues(self.elementQuadraturePoints,
                                                 self.q['x'])
        self.u[0].femSpace.elementMaps.getBasisValuesRef(
            self.elementQuadraturePoints)
        self.u[0].femSpace.elementMaps.getBasisGradientValuesRef(
            self.elementQuadraturePoints)
        self.u[0].femSpace.getBasisValuesRef(self.elementQuadraturePoints)
        self.u[0].femSpace.getBasisGradientValuesRef(
            self.elementQuadraturePoints)
        self.coefficients.initializeElementQuadrature(
            self.timeIntegration.t, self.q)
        if self.stabilization is not None:
            self.stabilization.initializeElementQuadrature(
                self.mesh, self.timeIntegration.t, self.q)
            self.stabilization.initializeTimeIntegration(self.timeIntegration)
        if self.shockCapturing is not None:
            self.shockCapturing.initializeElementQuadrature(
                self.mesh, self.timeIntegration.t, self.q)

    def calculateElementBoundaryQuadrature(self):
        pass

    def calculateExteriorElementBoundaryQuadrature(self):
        """
        Calculate the physical location and weights of the quadrature rules
        and the shape information at the quadrature points on global element boundaries.

        This function should be called only when the mesh changes.
        """
        #
        # get physical locations of element boundary quadrature points
        #
        # assume all components live on the same mesh
        self.u[0].femSpace.elementMaps.getBasisValuesTraceRef(
            self.elementBoundaryQuadraturePoints)
        self.u[0].femSpace.elementMaps.getBasisGradientValuesTraceRef(
            self.elementBoundaryQuadraturePoints)
        self.u[0].femSpace.getBasisValuesTraceRef(
            self.elementBoundaryQuadraturePoints)
        self.u[0].femSpace.getBasisGradientValuesTraceRef(
            self.elementBoundaryQuadraturePoints)
        self.u[0].femSpace.elementMaps.getValuesGlobalExteriorTrace(
            self.elementBoundaryQuadraturePoints, self.ebqe['x'])
        self.fluxBoundaryConditionsObjectsDict = dict([(cj, FluxBoundaryConditions(self.mesh,
                                                                                   self.nElementBoundaryQuadraturePoints_elementBoundary,
                                                                                   self.ebqe[('x')],
                                                                                   self.advectiveFluxBoundaryConditionsSetterDict[cj],
                                                                                   self.diffusiveFluxBoundaryConditionsSetterDictDict[cj]))
                                                       for cj in list(self.advectiveFluxBoundaryConditionsSetterDict.keys())])
        self.coefficients.initializeGlobalExteriorElementBoundaryQuadrature(
            self.timeIntegration.t, self.ebqe)

    def estimate_mt(self):
        pass

    def calculateSolutionAtQuadrature(self):
        pass

    def calculateAuxiliaryQuantitiesAfterStep(self):
        pass

    def updateAfterMeshMotion(self):
        pass
