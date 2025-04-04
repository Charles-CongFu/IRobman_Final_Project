import pybullet as p
import time

from typing import Any, Dict, Optional

from src.grasping.grasp_generation import GraspGeneration
from src.ik_solver.ik_solver import DifferentialIKSolver
from src.path_planning.simple_planning import SimpleTrajectoryPlanner



class GraspExecution:
    """Robot grasping execution class, responsible for planning and executing complete grasping actions"""
    
    def __init__(self, sim, config: Dict[str, Any], bbox_center, bbox_rotation_matrix):
        """
        Initialize grasping executor
        
        Parameters:
            sim: Simulation environment object
        """
        self.sim = sim
        self.ik_solver = DifferentialIKSolver(sim.robot.id, sim.robot.ee_idx, damping=0.05)
        self.trajectory_planner = SimpleTrajectoryPlanner
        self.config = config
        self.bbox_center = bbox_center
        self.bbox_rotation_matrix = bbox_rotation_matrix
    
    def execute_grasp(self, pose1_pos, pose1_orn, pose2_pos, pose2_orn):
        """
        Execute complete grasping process
        
        Parameters:
            best_grasp: best grasp pose (R, grasp_center)
            grasp_poses: optional pre-calculated poses (pose1_pos, pose1_orn, pose2_pos, pose2_orn)
            
        Returns:
            bool: True if grasping is successful, False otherwise
        """
        
        # get current robot joint angles
        start_joints = self.sim.robot.get_joint_positions()
        
        # Solve IK for pre-grasp position
        target_joints = self.ik_solver.solve(pose1_pos, pose1_orn, start_joints, max_iters=50, tolerance=0.001)
        
        if target_joints is None:
            print("IK cannot be solved, cannot move to pre-grasp position")
            return False
        
        # Generate and execute trajectory to pre-grasp position
        trajectory = self.trajectory_planner.generate_joint_trajectory(start_joints, target_joints, steps=100)
        self._execute_trajectory(trajectory, sim_steps_per_point=1)
        
        # Open gripper
        self.open_gripper()
        
        # Move to final grasp position
        current_joints = self.sim.robot.get_joint_positions()
        pose2_trajectory = self.trajectory_planner.generate_cartesian_trajectory(
            self.sim.robot.id, 
            self.sim.robot.arm_idx, 
            self.sim.robot.ee_idx,
            current_joints, 
            pose2_pos, 
            pose2_orn, 
            steps=100
        )
        
        if not pose2_trajectory:
            print("Cannot generate trajectory to final grasp position")
            return False
        
        self._execute_trajectory(pose2_trajectory, sim_steps_per_point=3)
        
        # Wait for stabilization
        self._wait(0.5)
        
        # Close gripper to grasp object
        self.close_gripper()
        
    def _execute_trajectory(self, trajectory, sim_steps_per_point=1):
        """Execute trajectory
        
        Parameters:
        trajectory: List of joint target positions
        speed: Time step between simulation steps (smaller = faster, higher = slower)
            Default 1/240 matches Bullet's default time step
        """
        for joint_target in trajectory:
            # set joint target position
            self.sim.robot.position_control(joint_target)
            
            # execute multiple simulation steps to ensure smooth movement
            for _ in range(sim_steps_per_point):
                self.sim.step()
                time.sleep(1/240.0)  # match the default step of the simulation
    
    def _wait(self, seconds):
        """Wait for specified seconds"""
        steps = int(seconds * 240)
        for _ in range(steps):
            self.sim.step()
            time.sleep(1/240.)
    
    def open_gripper(self, width=0.04):
        """Open robot gripper"""
        p.setJointMotorControlArray(
            self.sim.robot.id,
            jointIndices=self.sim.robot.gripper_idx,
            controlMode=p.POSITION_CONTROL,
            targetPositions=[width, width]
        )
        self._wait(0.5)
    
    def close_gripper(self, target_width=0.005, max_force=100.0):
        """Use mixed position and force control to close the gripper"""
        p.setJointMotorControlArray(
            self.sim.robot.id,
            jointIndices=self.sim.robot.gripper_idx,
            controlMode=p.POSITION_CONTROL,
            targetPositions=[target_width, target_width],
            forces=[max_force, max_force]
        )
        self._wait(1.0)
    
    def lift_object(self, height=0.5):
        """Grasp object and lift it to specified height"""
        # Get current end-effector position and orientation
        current_ee_pos, current_ee_orn = self.sim.robot.get_ee_pose()
        
        # Calculate lifted position
        lift_pos = current_ee_pos.copy()
        lift_pos[2] += height
        
        # Get current joint angles
        current_joints = self.sim.robot.get_joint_positions()
        
        # Solve IK for lifted position
        lift_target_joints = self.ik_solver.solve(lift_pos, current_ee_orn, current_joints, max_iters=50, tolerance=0.001)
        
        if lift_target_joints is None:
            print("IK cannot be solved for lifted position, cannot lift object")
            return False
        
        # Generate and execute lifting trajectory
        lift_trajectory = self.trajectory_planner.generate_joint_trajectory(current_joints, lift_target_joints, steps=100)
        
        if not lift_trajectory:
            print("Cannot generate lifting trajectory")
            return False
        
        self._execute_trajectory(lift_trajectory, sim_steps_per_point=5)
        return True

    def execute_complete_grasp(self, point_clouds, visualize=True, object_name: Optional[str] = None):
        """
        Execute complete process of grasping planning and execution
        
        Parameters:
        point_clouds: Collected point cloud data
        visualize: Whether to visualize grasping process
        
        Returns:
        success: True if grasping is successful, False otherwise
        self: Grasping executor object (if grasping is successful)
        """        
        grasp_generator = GraspGeneration(self.bbox_center, self.bbox_rotation_matrix, self.sim)
        pose1_pos, pose1_orn, pose2_pos, pose2_orn = grasp_generator.final_compute_poses(point_clouds, visualize, object_name)
        # Execute grasping (pass calculated pose)
        print("\nStarting to execute grasping...")
        self.execute_grasp(pose1_pos, pose1_orn, pose2_pos, pose2_orn)

        lift_success = self.lift_object()

        is_success = self.is_grasped()

        if is_success and lift_success:
            print("\nGrasping successful!")
        else:
            print("\nGrasping failed...")
            return False, False
        
        return True, True

    def is_grasped(self):
        target_width = 0.015 # when one grasp failed, the gripper closing width is 0.015
        
        # get the current position of the gripper joint
        gripper_joint_states = []
        for joint_idx in self.sim.robot.gripper_idx:
            joint_state = p.getJointState(self.sim.robot.id, joint_idx)
            gripper_joint_states.append(joint_state[0])  # joint_state[0] is the joint position
        
        # calculate the actual distance of the gripper
        actual_width = sum(gripper_joint_states)
        
        if actual_width < target_width:
            print("Warning: No object grasped")
            return False
        else:
            print("Grasp successful")
            print(f"Gripper closing width: {target_width}")
            print(f"Gripper actual width: {actual_width}")
            return True