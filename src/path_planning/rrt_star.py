import numpy as np
import pybullet as p
import random

from scipy.spatial import KDTree
from typing import List, Tuple

from src.robot import Robot
from src.obstacle_tracker.obstacle_tracker import ObstacleTracker



class RRTStarPlanner:
    """RRT* path planning algorithm for robotic arm.
    
    Plans in joint space while performing collision detection in Cartesian space.
    
    Args:
        robot: Instance of Robot class
        obstacle_tracker: Instance of ObstacleTracker to get obstacle positions
        max_iterations: Maximum number of RRT* iterations
        step_size: Maximum step size for extending the tree
        goal_sample_rate: Probability of sampling the goal
        search_radius: Radius for rewiring in RRT*
        goal_threshold: Distance threshold to consider goal reached (joint space)
        collision_check_step: Step size for collision checking along the path
    """
    def __init__(
        self,
        robot: Robot,
        obstacle_tracker: ObstacleTracker,
        max_iterations: int = 1000,
        step_size: float = 0.2,
        goal_sample_rate: float = 0.05,
        search_radius: float = 0.5,
        goal_threshold: float = 0.1,
        collision_check_step: float = 0.05
    ):
        self.robot = robot
        self.obstacle_tracker = obstacle_tracker
        self.max_iterations = max_iterations
        self.step_size = step_size
        self.goal_sample_rate = goal_sample_rate
        self.search_radius = search_radius
        self.goal_threshold = goal_threshold
        self.collision_check_step = collision_check_step
        
        self.dimension = len(robot.arm_idx)
        self.nodes = []  # List of nodes in the tree
        self.costs = []  # Cost from start to each node
        self.parents = []  # Parent index for each node
        
        # Visualization
        self.debug_lines = []
        
    def _get_current_ee_pose(self, joint_positions: List[float]) -> Tuple[np.ndarray, np.ndarray]:
        """Get end-effector pose for given joint positions.
        
        Args:
            joint_positions: Joint positions
            
        Returns:
            Tuple of end-effector position and orientation
        """
        # Save current state
        current_states = []
        for i in self.robot.arm_idx:
            current_states.append(p.getJointState(self.robot.id, i)[0])
            
        # Set joint positions
        for i, idx in enumerate(self.robot.arm_idx):
            p.resetJointState(self.robot.id, idx, joint_positions[i])
            
        # Get EE pose
        ee_state = p.getLinkState(self.robot.id, self.robot.ee_idx)
        ee_pos = np.array(ee_state[0])
        ee_orn = np.array(ee_state[1])
        
        # Restore original state
        for i, idx in enumerate(self.robot.arm_idx):
            p.resetJointState(self.robot.id, idx, current_states[i])
            
        return ee_pos, ee_orn
    
    def clear_visualization(self) -> None:
        """Clear visualization of tree."""
        for _, _, debug_id in self.debug_lines:
            p.removeUserDebugItem(debug_id)
        self.debug_lines = []
        
    def generate_smooth_trajectory(self, path: List[List[float]], smoothing_steps: int = 10) -> List[List[float]]:
        """Generate smooth trajectory from path.
        
        Args:
            path: Path as list of joint configurations
            smoothing_steps: Number of steps between each pair of configurations
            
        Returns:
            Smooth trajectory as list of joint configurations
        """
        if not path or len(path) < 2:
            return path
            
        # Interpolate between waypoints
        smooth_trajectory = []
        
        for i in range(len(path) - 1):
            start = path[i]
            end = path[i + 1]
            
            for step in range(smoothing_steps + 1):
                t = step / smoothing_steps
                interpolated = [start[j] + t * (end[j] - start[j]) for j in range(len(start))]
                smooth_trajectory.append(interpolated)
                
        return smooth_trajectory
    
    def _find_nearest(self, point: List[float]) -> int:
        """Find nearest node to point.
        
        Args:
            point: Query point
            
        Returns:
            Index of nearest node
        """
        dists = [np.linalg.norm(point - node) for node in self.nodes]
        return dists.index(min(dists))
    
    def _find_nearby(self, point: List[float]) -> List[int]:
            """Find nearby nodes within search radius.
            
            Args:
                point: Query point
                
            Returns:
                List of indices of nearby nodes
            """
            # Use KDTree for efficient nearest neighbor search
            kd_tree = KDTree(self.nodes)
            # Query for neighbors within radius
            indices = kd_tree.query_ball_point(point, self.search_radius)
            return indices

    def _is_state_in_collision(self, joint_pos: List[float]) -> bool:
        """Check if a joint state is in collision with obstacles.
        
        Args:
            joint_pos: Joint positions to check
            
        Returns:
            True if in collision, False otherwise
        """       
        # Get robot links' positions for collision checking
        # We'll check a few key links along the robot's kinematic chain
        links_to_check = self.robot.arm_idx + [self.robot.ee_idx]
        
        # Save current state
        current_states = []
        for i in self.robot.arm_idx:
            current_states.append(p.getJointState(self.robot.id, i)[0])
            
        # Set joint positions
        for i, idx in enumerate(self.robot.arm_idx):
            p.resetJointState(self.robot.id, idx, joint_pos[i])
        
        # Check collision with obstacles    
        collision = False
        
        # Get obstacle states from tracker
        obstacle_states = self.obstacle_tracker.get_all_obstacle_states()
        
        if obstacle_states is None or len(obstacle_states) == 0:
            # No detection needed, restore original state
            for i, idx in enumerate(self.robot.arm_idx):
                p.resetJointState(self.robot.id, idx, current_states[i])
            return False
        
        # Check each link against each obstacle
        for link_idx in links_to_check:
            link_state = p.getLinkState(self.robot.id, link_idx)
            link_pos = np.array(link_state[0])
            
            for obstacle in obstacle_states:
                if obstacle is None:
                    continue
                
                # Simple sphere collision check
                obstacle_pos = obstacle['position']
                obstacle_radius = obstacle['radius']
                
                # Distance between link and obstacle center
                dist = np.linalg.norm(link_pos - obstacle_pos)
                
                # Approximate the robot link as a point (simplification)
                # Add a small safety margin (0.05m)
                if dist < obstacle_radius + 0.05:
                    collision = True
                    break
                
        # Restore original state
        for i, idx in enumerate(self.robot.arm_idx):
            p.resetJointState(self.robot.id, idx, current_states[i])
            
        return collision
    
    def _is_ee_height_valid(self, joint_pos: List[float]) -> bool:
        """Check if end effector height is valid (above the table).
        
        Args:
            joint_pos: Joint positions to check
            
        Returns:
            True if end effector height is valid, False otherwise
        """
        # Get end-effector position
        ee_pos, _ = self._get_current_ee_pose(joint_pos)
        
        # Get robot base position (assuming it's at index 0)
        # We can access the robot base position or use a fixed threshold for table height
        # Here, we'll use a simple approach to check if ee_pos[2] (z-coordinate) is above a threshold
        
        # Get base link position
        # base_pos = p.getBasePositionAndOrientation(self.robot.id)[0]
        base_height = self.robot.pos[2]
        
        # Add a small threshold to account for the base height itself
        table_height = base_height + 0.01  # 1cm margin below base
        
        # Check if end effector is above the table height
        return ee_pos[2] > table_height
   
    def _is_collision_free(self, joints: List[float]) -> bool:
        """Check if a joint configuration is collision-free.
        
        Args:
            joints: Joint configuration
        """
        return self._is_ee_height_valid(joints) and not self._is_state_in_collision(joints)
    
    def _sample_random_config(self) -> List[float]:
        """Sample random joint configuration.
        
        Returns:
            Random joint configuration
        """
        # Try to sample valid configuration that doesn't put end effector below base height
        max_attempts = 50  # Maximum number of attempts to find valid configuration
        
        for _ in range(max_attempts):
            # Sample random joint configuration
            config = [random.uniform(low, high) for low, high in zip(self.robot.lower_limits, self.robot.upper_limits)]
            
            # Check if this configuration keeps the end effector above the table
            if self._is_collision_free(config):
                return config
                
        # If we couldn't find a valid configuration after max_attempts, 
        # return the last sampled configuration and let collision checking handle it
        print("Warning: Could not sample configuration with valid end effector height")
        return [random.uniform(low, high) for low, high in zip(self.robot.lower_limits, self.robot.upper_limits)]
    
    def _steer(self, from_config: List[float], to_config: List[float]) -> List[float]:
        """Steer from one configuration toward another with step size limit.
        
        Args:
            from_config: Starting joint configuration
            to_config: Target joint configuration
            
        Returns:
            New configuration after stepping toward target
        """
        dist = np.linalg.norm(to_config - from_config)
        
        if dist < self.step_size:
            # If directly reaching to_config, check height validity
            if self._is_collision_free(to_config):
                return to_config
            else:
                return from_config
        else:
            dir_vec = (to_config - from_config) / dist
            new_config = from_config + self.step_size * dir_vec
            
            # Check height validity of new_config
            if self._is_collision_free(new_config):
                return new_config
            else:
                return from_config
    
    def _choose_parent(self, new_node: List[float], nearby_indices: List[int]) -> Tuple[int, float]:
        """Choose best parent for new node from nearby nodes.
        
        Args:
            new_node: New node to find parent for
            nearby_indices: Indices of nearby nodes
            
        Returns:
            Tuple of (best parent index, cost to new node)
        """
        if not nearby_indices:
            return -1, float('inf')
            
        costs = []
        for idx in nearby_indices:
            # Cost from start to potential parent
            cost_to_parent = self.costs[idx]
            # Cost from parent to new node
            cost_to_new = np.linalg.norm(self.nodes[idx] - new_node)
            
            # Check if path is collision-free
            if self._is_collision_free(new_node):
                # Total cost
                costs.append((idx, cost_to_parent + cost_to_new))
            else:
                costs.append((idx, float('inf')))
                
        # sort by cost
        costs.sort(key=lambda x: x[1])
        
        # Make sure not all nearby nodes are filtered out, and no collision
        if costs and costs[0][1] < float('inf'): 
            return costs[0]
        else:
            return -1, float('inf')
    
    def _rewire(self, new_node_idx: int, nearby_indices: List[int]) -> None:
        """Rewire the tree to potentially improve paths.
        
        Args:
            new_node_idx: Index of new node
            nearby_indices: Indices of nearby nodes
        """
        new_node = self.nodes[new_node_idx]
        
        for idx in nearby_indices:
            if idx == self.parents[new_node_idx]:
                continue
                
            # Check if better path exists through new node
            cost_through_new = self.costs[new_node_idx] + np.linalg.norm(self.nodes[idx] - new_node)
            
            if cost_through_new < self.costs[idx]:
                # Check if path is collision-free
                if self._is_collision_free(new_node):
                    # Update parent and cost
                    self.parents[idx] = new_node_idx
                    self.costs[idx] = cost_through_new
                    
                    # Update visualization
                    self._update_visualization(idx)

    def _update_visualization(self, node_idx: int) -> None:
        """Update visualization of tree.
        
        Args:
            node_idx: Index of node to update
        """
        if self.parents[node_idx] == -1:
            return
            
        parent_idx = self.parents[node_idx]
        
        # Remove old visualization
        for i, (start, end) in enumerate(self.debug_lines):
            if np.array_equal(end, self.nodes[node_idx]):
                p.removeUserDebugItem(self.debug_lines[i][2])
                self.debug_lines.pop(i)
                break
                
        # Add new visualization
        start_ee, _ = self._get_current_ee_pose(self.nodes[parent_idx])
        end_ee, _ = self._get_current_ee_pose(self.nodes[node_idx])
        
        debug_id = p.addUserDebugLine(
            start_ee, end_ee, [0, 1, 0], 2, 0
        )
        
        self.debug_lines.append((self.nodes[parent_idx], self.nodes[node_idx], debug_id))
    
    def _extract_path(self, goal_idx: int) -> List[List[float]]:
            """Extract path from start to goal.
            
            Args:
                goal_idx: Index of goal node
                
            Returns:
                Path as list of joint configurations
            """
            path = []
            current = goal_idx
            
            while current != -1:  # -1 means no parent (start node)
                path.append(self.nodes[current])
                current = self.parents[current]
                
            return path[::-1]  # Reverse to get path from start to goal

    def plan(self, start_config: List[float], goal_config: List[float]) -> Tuple[List[List[float]], float]:
        """Plan a path from start to goal configuration.
        
        Args:
            start_config: Starting joint configuration
            goal_config: Goal joint configuration
            
        Returns:
            Tuple of (path as list of joint configurations, path cost)
        """
        print("Starting RRT* planning with base height constraint...")
            
        # Initialize RRT* tree
        self.nodes = [start_config]
        self.costs = [0.0]
        self.parents = [-1]  # no parent for start node
        self.debug_lines = []
        
        # RRT* main loop
        for i in range(self.max_iterations):
            if i % 100 == 0:
                print(f"RRT* planning iteration {i}/{self.max_iterations}")
                
            # Sample random configuration (with bias toward goal)
            if random.random() < self.goal_sample_rate:
                random_config = goal_config
            else:
                random_config = self._sample_random_config()
                
            # Find nearest node
            nearest_idx = self._find_nearest(random_config)
            
            # Steer toward random config
            new_config = self._steer(self.nodes[nearest_idx], random_config)
                
            # Find nearby nodes
            nearby_indices = self._find_nearby(new_config)
            
            # Choose best parent
            best_parent_idx, cost_to_new = self._choose_parent(new_config, nearby_indices)
            
            if best_parent_idx == -1:
                # No valid parent found
                continue
                
            # Add node to the tree
            self.nodes.append(new_config)
            self.costs.append(cost_to_new)
            self.parents.append(best_parent_idx)
            new_node_idx = len(self.nodes) - 1
            
            # Add visualization
            start_ee, _ = self._get_current_ee_pose(self.nodes[best_parent_idx])
            end_ee, _ = self._get_current_ee_pose(new_config)
            
            debug_id = p.addUserDebugLine(
                start_ee, end_ee, [0, 1, 0], 2, 0
            )
            
            self.debug_lines.append((self.nodes[best_parent_idx], new_config, debug_id))
            
            # Rewire the tree
            self._rewire(new_node_idx, nearby_indices)
            
            # Check if we've reached the goal
            if np.linalg.norm(np.array(goal_config) - np.array(new_config)) < self.goal_threshold:
                print(f"Goal reached after {i+1} iterations!")
                
                # Add goal node if not already part of the tree
                if np.linalg.norm(np.array(new_config) - np.array(goal_config)) > 1e-6 and self._is_collision_free(goal_config):
                    # Add goal node
                    self.nodes.append(goal_config)
                    cost_to_goal = cost_to_new + np.linalg.norm(np.array(goal_config) - np.array(new_config))
                    self.costs.append(cost_to_goal)
                    self.parents.append(new_node_idx)
                    new_node_idx = len(self.nodes) - 1
                    
                    # Add visualization
                    start_ee, _ = self._get_current_ee_pose(self.nodes[best_parent_idx])
                    end_ee, _ = self._get_current_ee_pose(new_config)
                    
                    debug_id = p.addUserDebugLine(
                        start_ee, end_ee, [0, 1, 0], 2, 0
                    )
                    
                    self.debug_lines.append((self.nodes[best_parent_idx], new_config, debug_id))
                    
                    # Rewire the tree
                    self._rewire(new_node_idx, nearby_indices)
                    
                   
                    goal_idx = new_node_idx
                else:
                    goal_idx = new_node_idx
                    
                # Extract path
                path = self._extract_path(goal_idx)
                path_cost = self.costs[goal_idx]
                
                return path, path_cost
        
        # Try to find closest node to goal
        dists_to_goal = [np.linalg.norm(node - goal_config) for node in self.nodes]
        closest_idx = dists_to_goal.index(min(dists_to_goal))
        
        # Extract path to closest node
        path = self._extract_path(closest_idx)
        path_cost = self.costs[closest_idx]
        
        return path, path_cost
    
